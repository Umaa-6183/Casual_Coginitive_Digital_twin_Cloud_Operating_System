# ADR-005: Claude claude-sonnet-4 as the Operator Co-Pilot

**Status**: Accepted  
**Date**: 2024-12-20  
**Authors**: CCDT Platform Engineering

---

## Context

We needed a natural-language interface for operators to understand incidents and approve/override remediation actions. Requirements:
- Summarise complex GNN inference results in plain English
- Explain why a specific action was selected
- Allow operators to ask follow-up questions ("show me the logs", "what's the blast radius")
- Call internal tools (topology, ghost preview, log fetch)
- Generate structured incident reports
- Operate within a multi-turn session context per operator

## Decision

Use **Anthropic Claude claude-sonnet-4** via the Messages API with:
- Tool use (function calling) for 4 internal tools
- System prompt injection of current cluster state on every request
- Rolling 20-turn conversation window (manages token budget)
- Server-Sent Events (SSE) streaming for low time-to-first-token
- Session persistence across Co-Pilot restarts via proto serialization

### System Prompt Strategy
On every API call, CCDT injects:
1. Current GNN inference result (structured JSON)
2. Recent action history (last 5 actions)
3. Cluster topology summary (top 10 most anomalous nodes)
4. Available tools and their schemas

### Tool Registry
| Tool | Purpose |
|---|---|
| `get_topology` | Fetch current service graph with anomaly scores |
| `get_ebpf_events` | Fetch recent kernel events for a specific pod |
| `run_ghost_preview` | Simulate an action without executing it |
| `propose_action` | Send a remediation proposal to Guardian |

### Fine-Tuning Loop
Every resolved incident generates a `FinetuningExample` that captures the full conversation, the final action taken, and the outcome. High-quality examples (human_approved=True, operator_rating≥4, action_resolved_incident=True) are used to fine-tune future model versions.

## Consequences

**Positive**
- Operators get plain-English explanations of complex causal chains
- Tool use lets the LLM fetch live data rather than relying on stale context
- Streaming SSE means first token arrives in < 3s even for long responses
- Fine-tuning loop continuously improves the model's incident-specific knowledge

**Negative**
- Claude API dependency: if Anthropic has an outage, Co-Pilot is unavailable
  → mitigated: other 3 layers continue operating autonomously
- Token costs: ~$0.01–$0.05 per incident conversation
- Context window management: 20-turn rolling window may lose early context

## Alternatives Considered

**GPT-4 Turbo**: Comparable quality, but Anthropic's Constitutional AI and tool use implementation is more robust for safety-critical contexts.

**Fine-tuned open-source LLM (Llama-3)**: Lower ongoing cost, but requires GPU hosting, worse tool use, and higher maintenance burden.

**Rule-based chatbot**: Cannot handle novel questions or explain causal chains.

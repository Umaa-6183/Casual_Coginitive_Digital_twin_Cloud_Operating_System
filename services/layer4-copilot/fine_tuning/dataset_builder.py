"""
CCDT Layer-4 Co-Pilot — Fine-Tuning Dataset Builder
════════════════════════════════════════════════════════════════════════════════
Builds a Supervised Fine-Tuning (SFT) dataset of multi-turn conversations
from CCDT incident history. The resulting dataset is used to fine-tune or
continue-pretrain a smaller open-source model (Mistral-7B or Llama-3-8B)
to act as a cost-efficient fallback co-pilot.

Data sources:
  1. incident_history.jsonl     Historical incidents with root cause, timeline,
                                 guardian actions, and post-mortem notes
  2. topology_snapshots/        Topology JSON files captured at incident time
  3. ebpf_event_logs/           Raw eBPF event sequences

Output format (HuggingFace SFTTrainer / ChatML):
  Each record = one full multi-turn conversation:
  {
    "messages": [
      {"role": "system",    "content": "<system prompt>"},
      {"role": "user",      "content": "<SRE question>"},
      {"role": "assistant", "content": "<ideal answer>"}
    ]
  }

Dataset splits:
  train  80%
  val    10%
  test   10%

Usage:
  python fine_tuning/dataset_builder.py \\
      --incidents /data/incidents/ \\
      --output    /data/sft_dataset/ \\
      --max-per-incident 5
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ccdt.fine_tuning.dataset_builder")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ─── Constants ────────────────────────────────────────────────────────────────
SYSTEM_PROMPT_FT = """\
You are CCDT Co-Pilot, an expert AI SRE assistant for Kubernetes infrastructure.
You analyze eBPF telemetry, GNN causal inferences, and Guardian remediation
actions to diagnose incidents and recommend remediations. Be precise, cite
evidence, and always quantify your confidence."""

# ─── Conversation templates ───────────────────────────────────────────────────
# Each template is (question_fn, answer_fn) — both receive an incident dict.

def _q_root_cause(inc: dict) -> str:
    return f"What is the root cause of the current incident? Our GNN detected a {inc.get('incident_type','fault')} with root cause node {inc.get('root_cause_node','unknown')}."

def _a_root_cause(inc: dict) -> str:
    chain  = inc.get("causal_chain", [])
    chain_str = " → ".join(c if isinstance(c, str) else c.get("node","?") for c in chain[:5])
    conf   = inc.get("root_cause_confidence", 0)
    blast  = inc.get("blast_radius", [])
    ebpf   = inc.get("ebpf_signals", [])
    signal_str = "; ".join(ebpf[:3]) if ebpf else "none captured"
    return (
        f"**Root Cause:** `{inc.get('root_cause_node', 'unknown')}` "
        f"(confidence: {conf:.0%})\n\n"
        f"**Causal chain:** {chain_str or 'not available'}\n\n"
        f"**eBPF evidence:** {signal_str}\n\n"
        f"**Blast radius:** {', '.join(blast) if blast else 'none'} ({len(blast)} nodes affected)\n\n"
        f"This is classified as a **{inc.get('incident_type','fault')}** incident. "
        f"{'The eBPF signals (capability escalation, suspicious syscalls) confirm adversarial activity.' if inc.get('incident_type') == 'attack' else 'The pattern matches a performance degradation cascade starting at the root-cause node.'}"
    )

def _q_remediation(inc: dict) -> str:
    return f"What remediation actions should we take for this {inc.get('incident_type','fault')} incident affecting {inc.get('root_cause_node','unknown')}?"

def _a_remediation(inc: dict) -> str:
    actions   = inc.get("guardian_actions", [])
    inc_type  = inc.get("incident_type", "fault")
    root      = inc.get("root_cause_node", "unknown")

    if inc_type == "attack":
        rec = (
            f"**PROPOSED ACTION:** `isolate_container` on `{root}` first — "
            f"network-isolate the compromised pod to contain lateral movement before any restart.\n\n"
            f"**Then:** `rotate_secrets` — revoke credentials the attacker may have exfiltrated.\n\n"
            f"**Then:** `apply_network_policy` — deny all ingress to the namespace.\n\n"
            f"**Do NOT:** rollback the deployment until the pod is isolated — rolling back an "
            f"attack-compromised image can restore the attacker's foothold."
        )
    else:
        rec = (
            f"**PROPOSED ACTION:** `rollback_deployment` on `{root}` — "
            f"the fault pattern suggests a bad deployment. Rollback is low-risk and fast.\n\n"
            f"**If OOM:** `increase_oom_threshold` to relieve memory pressure immediately.\n\n"
            f"**If CPU saturation:** `scale_up_replicas` to distribute load while rollback completes."
        )

    if actions:
        taken = [a.get("name", a) if isinstance(a, dict) else a for a in actions[:3]]
        rec += f"\n\n**Guardian already took:** {', '.join(taken)}"

    return rec

def _q_impact(inc: dict) -> str:
    return "What is the user and business impact of this incident?"

def _a_impact(inc: dict) -> str:
    blast    = inc.get("blast_radius", [])
    inc_type = inc.get("incident_type", "fault")
    mttr     = inc.get("mttr_minutes", "unknown")
    sev      = inc.get("severity", "high")

    sev_map = {
        "critical": "complete service outage with potential data loss",
        "high":     "significant degradation — error rates above 5% for affected services",
        "medium":   "partial degradation — increased latency for some users",
        "low":      "minor degradation — internal services affected, user-facing SLA intact",
    }
    impact = sev_map.get(sev, "degraded service quality")

    data_risk = ""
    if inc_type == "attack":
        data_risk = "\n\n⚠️ **Security impact:** Potential data exfiltration risk. Rotate secrets and audit access logs immediately."

    return (
        f"**Severity:** {sev.upper()}\n"
        f"**Affected nodes:** {', '.join(blast) if blast else 'unknown'} ({len(blast)} total)\n"
        f"**User impact:** {impact}\n"
        f"**Estimated MTTR:** {mttr} minutes"
        f"{data_risk}"
    )

def _q_explain_gnn(inc: dict) -> str:
    return "Can you explain what the GNN causal analysis found and how confident it is?"

def _a_explain_gnn(inc: dict) -> str:
    chain   = inc.get("causal_chain", [])
    conf    = inc.get("root_cause_confidence", 0.0)
    gclass  = inc.get("graph_classification", {})
    nc      = inc.get("node_classifications", {})

    chain_strs = []
    for c in chain[:5]:
        node  = c.get("node", c) if isinstance(c, dict) else c
        score = c.get("causalScore", 0) if isinstance(c, dict) else 0
        chain_strs.append(f"`{node}` (causal score: {score:.2f})")

    nc_lines = []
    for node, probs in (nc or {}).items():
        if isinstance(probs, dict):
            dominant = max(probs, key=probs.get)
            if dominant != "healthy":
                nc_lines.append(f"  - `{node}`: {dominant} ({probs.get(dominant, 0):.0%})")

    return (
        f"The GNN analysed the full service topology graph using **4 layers of GATv2 attention** "
        f"with **8 attention heads** and **NOTEARS DAG regularisation** to learn the causal structure.\n\n"
        f"**Graph-level classification:** {gclass}\n\n"
        f"**Causal chain (ranked by causal score):**\n"
        + ("\n".join(f"  {i+1}. {s}" for i, s in enumerate(chain_strs)) or "  Not available")
        + f"\n\n**Root cause confidence:** {conf:.0%}\n\n"
        f"**Anomalous node classifications:**\n"
        + ("\n".join(nc_lines) if nc_lines else "  All nodes healthy")
        + f"\n\nConfidence above 85% is considered high-confidence. At {conf:.0%}, "
        f"{'we can act autonomously.' if conf >= 0.85 else 'human review is recommended before executing destructive actions.'}"
    )

def _q_prevention(inc: dict) -> str:
    return "How do we prevent this incident from happening again?"

def _a_prevention(inc: dict) -> str:
    inc_type = inc.get("incident_type", "fault")
    root     = inc.get("root_cause_node", "unknown")

    if inc_type == "attack":
        return (
            f"**Prevention Plan for Security Incident:**\n\n"
            f"1. **Harden `{root}`:** Apply PodSecurityPolicy/PSA to deny `privileged: true` "
            f"and drop all Linux capabilities. Only add back what the container actually needs.\n"
            f"2. **Enable LSM/eBPF enforcement:** Deploy the capability.bpf.c probe in enforcement "
            f"mode (currently detection-only) to block CAP_SYS_ADMIN acquisition at the kernel.\n"
            f"3. **Network segmentation:** Apply default-deny NetworkPolicies to all production "
            f"namespaces. Whitelist only required service-to-service paths.\n"
            f"4. **Image scanning:** Add Trivy/Grype to CI/CD. Block images with CRITICAL CVEs.\n"
            f"5. **Secret rotation schedule:** Rotate all service account tokens monthly. "
            f"Use external-secrets-operator with short TTLs (1 hour for privileged secrets).\n"
            f"6. **Incident response runbook:** Document the isolation→evidence→recovery sequence."
        )
    else:
        return (
            f"**Prevention Plan for Performance Fault:**\n\n"
            f"1. **Circuit breaker on `{root}`:** Implement retry limits + timeout on all callers. "
            f"A single slow database should not cascade to all upstream services.\n"
            f"2. **Resource limits:** Set explicit CPU/memory requests+limits on all containers. "
            f"OOM kills indicate missing memory limits.\n"
            f"3. **Readiness probes:** Tune K8s readiness probes to remove overloaded pods "
            f"from LB rotation faster (target: < 10s).\n"
            f"4. **Horizontal Pod Autoscaler:** Add HPA rules based on custom eBPF metrics "
            f"(sched latency P99 > 20ms → scale up).\n"
            f"5. **Chaos testing:** Run monthly chaos experiments (pod kill, memory stress) "
            f"to validate resilience.\n"
            f"6. **Deployment safeguards:** Add canary deployments + automated rollback triggers "
            f"based on error rate > 2%."
        )


TEMPLATES = [
    (_q_root_cause,    _a_root_cause),
    (_q_remediation,   _a_remediation),
    (_q_impact,        _a_impact),
    (_q_explain_gnn,   _a_explain_gnn),
    (_q_prevention,    _a_prevention),
]


# ─── Dataset builder ──────────────────────────────────────────────────────────

@dataclass
class ConversationRecord:
    """One SFT training record."""
    incident_id:  str
    messages:     list[dict]
    metadata:     dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id":       self.incident_id,
            "messages": self.messages,
            "metadata": self.metadata,
        }


class IncidentDatasetBuilder:
    """
    Builds a ChatML-format SFT dataset from CCDT incident history.

    Incident JSON schema (minimal required fields):
    {
        "id":                 "INC-2024-001",
        "incident_type":      "fault",           # fault | attack
        "root_cause_node":    "order-svc",
        "root_cause_confidence": 0.92,
        "causal_chain":       [{"node":"a","causalScore":0.9}, ...],
        "blast_radius":       ["order-svc", "postgres"],
        "severity":           "high",
        "mttr_minutes":       18,
        "ebpf_signals":       ["OOM kill on postgres", "TCP retransmit storm on order-svc"],
        "guardian_actions":   [{"name": "rollback_deployment", "status": "executed"}],
        "graph_classification": {"healthy":0.02, "fault":0.91, "attack":0.07},
        "node_classifications": {"order-svc": {"fault":0.94}, "postgres": {"fault":0.88}},
        "postmortem_notes":   "..."    # optional — used for multi-turn augmentation
    }
    """

    def __init__(
        self,
        max_per_incident:  int   = 5,
        val_ratio:         float = 0.10,
        test_ratio:        float = 0.10,
        seed:              int   = 42,
    ) -> None:
        self.max_per_incident = max_per_incident
        self.val_ratio        = val_ratio
        self.test_ratio       = test_ratio
        self._rng             = random.Random(seed)

    def build_from_dir(self, incidents_dir: str) -> dict[str, list[dict]]:
        """
        Load all .json incident files from incidents_dir, build conversations,
        split into train/val/test.

        Returns {"train": [...], "val": [...], "test": [...]}
        """
        incidents_path = Path(incidents_dir)
        json_files     = sorted(incidents_path.glob("**/*.json"))
        jsonl_files    = sorted(incidents_path.glob("**/*.jsonl"))

        incidents: list[dict] = []
        for f in json_files:
            try:
                data = json.loads(f.read_text())
                if isinstance(data, list):
                    incidents.extend(data)
                else:
                    incidents.append(data)
            except Exception as exc:
                logger.warning("Failed to load %s: %s", f, exc)

        for f in jsonl_files:
            try:
                for line in f.read_text().splitlines():
                    if line.strip():
                        incidents.append(json.loads(line))
            except Exception as exc:
                logger.warning("Failed to load %s: %s", f, exc)

        if not incidents:
            logger.warning("No incidents found in %s — using synthetic demo data", incidents_dir)
            incidents = self._generate_demo_incidents()

        logger.info("Loaded %d incidents", len(incidents))
        all_records = self._build_all_conversations(incidents)

        self._rng.shuffle(all_records)
        n     = len(all_records)
        n_val  = max(1, int(n * self.val_ratio))
        n_test = max(1, int(n * self.test_ratio))

        splits = {
            "test":  all_records[:n_test],
            "val":   all_records[n_test: n_test + n_val],
            "train": all_records[n_test + n_val:],
        }
        logger.info(
            "Dataset: train=%d  val=%d  test=%d  total=%d",
            len(splits["train"]), len(splits["val"]), len(splits["test"]), n,
        )
        return {k: [r.to_dict() for r in v] for k, v in splits.items()}

    def save(self, splits: dict[str, list[dict]], output_dir: str) -> None:
        """Save splits as JSONL files."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        for split_name, records in splits.items():
            path = out / f"{split_name}.jsonl"
            with path.open("w") as f:
                for rec in records:
                    f.write(json.dumps(rec) + "\n")
            logger.info("Saved %s: %d records → %s", split_name, len(records), path)

        # Write dataset card
        card = {
            "name":        "ccdt_copilot_sft",
            "description": "CCDT Co-Pilot SFT dataset — incident Q&A conversations",
            "splits":      {k: len(v) for k, v in splits.items()},
            "format":      "ChatML (messages list with system/user/assistant roles)",
            "base_model":  "Mistral-7B-v0.3 or Llama-3-8B-Instruct",
        }
        (out / "dataset_card.json").write_text(json.dumps(card, indent=2))
        logger.info("Dataset saved to %s", output_dir)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_all_conversations(
        self, incidents: list[dict]
    ) -> list[ConversationRecord]:
        records = []
        for inc in incidents:
            inc_records = self._incident_to_conversations(inc)
            records.extend(inc_records)
        return records

    def _incident_to_conversations(
        self, inc: dict
    ) -> list[ConversationRecord]:
        """Generate up to max_per_incident conversation records from one incident."""
        templates = list(TEMPLATES)
        self._rng.shuffle(templates)
        templates = templates[:self.max_per_incident]

        records = []
        inc_id  = inc.get("id", hashlib.md5(json.dumps(inc, sort_keys=True).encode()).hexdigest()[:8])

        for i, (q_fn, a_fn) in enumerate(templates):
            try:
                question = q_fn(inc)
                answer   = a_fn(inc)
            except Exception as exc:
                logger.debug("Template error for %s[%d]: %s", inc_id, i, exc)
                continue

            # Single-turn conversation
            messages = [
                {"role": "system",    "content": SYSTEM_PROMPT_FT},
                {"role": "user",      "content": question},
                {"role": "assistant", "content": answer},
            ]

            records.append(ConversationRecord(
                incident_id = f"{inc_id}_{i}",
                messages    = messages,
                metadata    = {
                    "incident_type": inc.get("incident_type", "fault"),
                    "severity":      inc.get("severity", "high"),
                    "template_idx":  i,
                },
            ))

        # Multi-turn conversation from postmortem notes (if present)
        pm = inc.get("postmortem_notes", "")
        if pm and len(pm) > 50:
            records.append(self._postmortem_to_multiturn(inc_id, inc, pm))

        return records

    def _postmortem_to_multiturn(
        self, inc_id: str, inc: dict, postmortem: str
    ) -> ConversationRecord:
        """Build a 3-turn conversation incorporating postmortem notes."""
        messages = [
            {"role": "system",    "content": SYSTEM_PROMPT_FT},
            {"role": "user",      "content": _q_root_cause(inc)},
            {"role": "assistant", "content": _a_root_cause(inc)},
            {"role": "user",      "content": "What were the key lessons from the postmortem?"},
            {"role": "assistant", "content": (
                f"Based on the postmortem analysis:\n\n{postmortem[:1500]}\n\n"
                + _a_prevention(inc)
            )},
        ]
        return ConversationRecord(
            incident_id = f"{inc_id}_postmortem",
            messages    = messages,
            metadata    = {"incident_type": inc.get("incident_type"), "template_idx": 99},
        )

    def _generate_demo_incidents(self) -> list[dict]:
        """Generate synthetic demo incidents for testing when no real data is available."""
        return [
            {
                "id": "DEMO-FAULT-001",
                "incident_type": "fault",
                "root_cause_node": "postgres",
                "root_cause_confidence": 0.92,
                "causal_chain": [
                    {"node": "postgres",  "causalScore": 0.92},
                    {"node": "order-svc", "causalScore": 0.84},
                    {"node": "notify-svc","causalScore": 0.61},
                ],
                "blast_radius": ["order-svc", "postgres", "notify-svc"],
                "severity": "critical",
                "mttr_minutes": 18,
                "ebpf_signals": [
                    "OOM kill on postgres (3 kills in 15 min)",
                    "TCP retransmit storm on order-svc (187 rps)",
                    "Scheduler latency spike on notify-svc (320ms P99)",
                ],
                "guardian_actions": [
                    {"name": "increase_oom_threshold", "status": "executed"},
                    {"name": "rollback_deployment",    "status": "executed"},
                ],
                "graph_classification": {"healthy": 0.02, "fault": 0.94, "attack": 0.04},
                "node_classifications": {
                    "postgres":   {"healthy": 0.03, "fault": 0.93, "attack": 0.04},
                    "order-svc":  {"healthy": 0.05, "fault": 0.89, "attack": 0.06},
                    "notify-svc": {"healthy": 0.15, "fault": 0.77, "attack": 0.08},
                },
                "postmortem_notes": (
                    "PostgreSQL ran out of shared_buffers due to a missing memory limit on the "
                    "pod. The bad deployment v2.4.1 introduced a query without an index. "
                    "Resolution: add memory limits, revert to v2.4.0, add missing index. "
                    "Future: add slow query alerting and enforce resource limits in CI."
                ),
            },
            {
                "id": "DEMO-ATTACK-001",
                "incident_type": "attack",
                "root_cause_node": "order-svc",
                "root_cause_confidence": 0.89,
                "causal_chain": [
                    {"node": "order-svc",  "causalScore": 0.89},
                    {"node": "postgres",   "causalScore": 0.71},
                    {"node": "notify-svc", "causalScore": 0.55},
                ],
                "blast_radius": ["order-svc", "postgres"],
                "severity": "critical",
                "mttr_minutes": 32,
                "ebpf_signals": [
                    "CAP_SYS_ADMIN acquisition on order-svc (5 events in 2 min)",
                    "pivot_root syscall detected (container escape attempt)",
                    "/var/run/docker.sock access (critical file access)",
                    "Outbound TCP to 198.51.100.42:4444 (C2 beacon)",
                ],
                "guardian_actions": [
                    {"name": "isolate_container",   "status": "executed"},
                    {"name": "rotate_secrets",       "status": "executed"},
                    {"name": "apply_network_policy", "status": "executed"},
                ],
                "graph_classification": {"healthy": 0.01, "fault": 0.12, "attack": 0.87},
                "node_classifications": {
                    "order-svc": {"healthy": 0.02, "fault": 0.09, "attack": 0.89},
                    "postgres":  {"healthy": 0.10, "fault": 0.72, "attack": 0.18},
                },
                "postmortem_notes": (
                    "Attacker exploited CVE-2024-XXXX in the order-svc image (outdated base). "
                    "Gained CAP_SYS_ADMIN, attempted container escape via pivot_root. "
                    "CCDT detected and isolated within 45 seconds of first capability event. "
                    "No data exfiltration confirmed. Resolution: patch image, add Falco rules, "
                    "implement PSA enforce mode on production namespace."
                ),
            },
        ]


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build CCDT Co-Pilot SFT dataset from incident history"
    )
    parser.add_argument(
        "--incidents", required=True,
        help="Directory containing incident JSON/JSONL files"
    )
    parser.add_argument(
        "--output", required=True,
        help="Output directory for train/val/test JSONL files"
    )
    parser.add_argument(
        "--max-per-incident", type=int, default=5,
        help="Max conversations generated per incident (default: 5)"
    )
    parser.add_argument(
        "--val-ratio", type=float, default=0.10,
        help="Validation split ratio (default: 0.10)"
    )
    parser.add_argument(
        "--test-ratio", type=float, default=0.10,
        help="Test split ratio (default: 0.10)"
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    builder = IncidentDatasetBuilder(
        max_per_incident = args.max_per_incident,
        val_ratio        = args.val_ratio,
        test_ratio       = args.test_ratio,
        seed             = args.seed,
    )

    splits = builder.build_from_dir(args.incidents)
    builder.save(splits, args.output)
    print(f"\n✅ Dataset saved to: {args.output}")
    print(f"   train={len(splits['train'])}  val={len(splits['val'])}  test={len(splits['test'])}")


if __name__ == "__main__":
    main()

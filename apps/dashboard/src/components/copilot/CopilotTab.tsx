import React, { useState, useRef, useEffect, useCallback } from 'react';
import { GhostPreviewModal } from '@/components/ghost/GhostPreviewModal';
import type { ChatMessage, GhostAction } from '@/types';

interface Props {
  onOpenGhost: (action: GhostAction) => void;
}

const QUICK_PROMPTS = [
  'Explain the root cause of INC-2847',
  'What is the blast radius of the current attack?',
  'Propose safe remediation for postgres OOM',
  'Compare risk: isolate vs restart for order-svc',
  'Generate incident report for INC-2847',
];

const MOCK_RESPONSES: Record<string, string> = {
  'Explain the root cause of INC-2847':
    `**Root Cause Analysis — INC-2847**

Based on causal GNN inference (confidence: **94.2%**), the attack chain is:

1. **CAP_SYS_ADMIN escalation** on \`order-svc\` (pid=7841) at 14:32:01
   - eBPF capability probe detected \`cap_capable(CAP_SYS_ADMIN)\` from uid=1000
   - Container security context lacked proper capability drop

2. **Memory manipulation** → PostgreSQL OOM kill at 14:32:03
   - Attacker leveraged admin cap to mmap large anonymous regions
   - PostgreSQL RSS reached 3.8GB against 4GB cgroup limit

3. **TCP retransmit storm** on order-svc → postgres edge (187/s)
   - Connection reset after OOM, causing repeated reconnect storm

4. **Cascade to notify-svc** — scheduler latency p99=142ms from shared node CPU starvation

**Attack classification**: Lateral movement → Memory exhaustion → Service disruption

PROPOSED ACTION: isolate_container on order-svc to contain lateral movement.`,

  'Propose safe remediation for postgres OOM':
    `**Remediation Plan — PostgreSQL OOM**

**Immediate (Low Risk, OPA PASS)**:
1. \`increase_memory_limit\` on postgres: 4GB → 6GB (risk 20/100, confidence 79%)
   - Ghost Preview: MTTR -45%, traffic -5%
2. \`apply_network_policy\` — block order-svc excessive connections (risk 15/100)

**Short-term (Medium Risk)**:
3. \`restart_pod\` postgres after memory increase stabilises (risk 25/100)
   - Recommend Ghost Preview validation first

**Prevention**:
4. Add OOM-guard cgroup limits with 20% headroom
5. Configure postgres \`max_connections=50\` (currently unlimited)

PROPOSED ACTION: increase_memory_limit on postgres`,
};

function simulateStream(text: string, onChunk: (s: string) => void, onDone: () => void) {
  const words = text.split(' ');
  let i = 0;
  const timer = setInterval(() => {
    if (i >= words.length) {
      clearInterval(timer);
      onDone();
      return;
    }
    onChunk(words[i] + ' ');
    i++;
  }, 45);
  return () => clearInterval(timer);
}

function detectProposedAction(text: string): GhostAction | null {
  const match = text.match(/PROPOSED ACTION:\s*(\w+)\s+on\s+([\w-]+)/);
  if (!match) return null;
  const actionMap: Record<string, string> = {
    isolate_container:   'isolate_container',
    increase_memory_limit: 'increase_memory_limit',
    restart_pod:         'restart_pod',
    apply_network_policy:'apply_network_policy',
    block_ip:            'block_ip',
  };
  const actionName = actionMap[match[1]] ?? match[1];
  return {
    label:      `${match[1]} on ${match[2]}`,
    icon:       '⚡',
    actionName,
    targetNode: match[2],
  };
}

// Simple markdown-ish renderer
function renderMarkdown(text: string) {
  const lines = text.split('\n');
  return lines.map((line, i) => {
    if (line.startsWith('**') && line.endsWith('**') && line.length > 4) {
      return <div key={i} style={{ fontWeight: 700, color: '#C8D8E8', marginTop: 8, marginBottom: 2 }}>{line.slice(2, -2)}</div>;
    }
    if (line.startsWith('**')) {
      // Inline bold
      const parts = line.split(/\*\*(.*?)\*\*/g);
      return (
        <div key={i} style={{ lineHeight: 1.6, marginBottom: 2 }}>
          {parts.map((p, j) => j % 2 === 1 ? <strong key={j} style={{ color: '#00D4FF' }}>{p}</strong> : p)}
        </div>
      );
    }
    if (line.startsWith('- ') || line.match(/^\d+\./)) {
      return <div key={i} style={{ paddingLeft: 16, lineHeight: 1.6, marginBottom: 1 }}>{line}</div>;
    }
    if (line.startsWith('PROPOSED ACTION:')) {
      return (
        <div key={i} style={{
          marginTop: 8, padding: '6px 10px', background: '#9B5DE522',
          border: '1px solid #9B5DE544', borderRadius: 6,
          color: '#9B5DE5', fontSize: 12, fontFamily: 'JetBrains Mono, monospace',
        }}>
          {line}
        </div>
      );
    }
    if (line.trim() === '') return <div key={i} style={{ height: 6 }} />;
    return <div key={i} style={{ lineHeight: 1.6, marginBottom: 2 }}>{line}</div>;
  });
}

export const CopilotTab: React.FC<Props> = () => {
  const [messages,    setMessages]    = useState<ChatMessage[]>([]);
  const [input,       setInput]       = useState('');
  const [streaming,   setStreaming]   = useState(false);
  const [ghostAction, setGhostAction] = useState<GhostAction | null>(null);
  const bottomRef  = useRef<HTMLDivElement>(null);
  const stopRef    = useRef<(() => void) | null>(null);
  const sessionId  = useRef(`session-${Date.now()}`);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streaming]);

  const send = useCallback((text: string) => {
    if (!text.trim() || streaming) return;
    const userMsg: ChatMessage = { role: 'user', content: text.trim(), ts: Date.now() };
    setMessages(prev => [...prev, userMsg]);
    setInput('');
    setStreaming(true);

    // Get mock response or default
    const response =
      MOCK_RESPONSES[text.trim()] ??
      `Analysing cluster state for: **"${text}"**\n\nBased on current GNN inference (root cause: order-svc, confidence 94.2%), the relevant context is:\n\n- 2 active critical alerts\n- Causal chain depth: 6 nodes\n- OPA compliance: 100%\n- MTTR target: 15min (current elapsed: 4:32)\n\nFor detailed analysis, please specify the incident ID or affected service.`;

    let accumulated = '';
    const stop = simulateStream(
      response,
      (chunk) => {
        accumulated += chunk;
        setMessages(prev => {
          const last = prev[prev.length - 1];
          if (last?.role === 'assistant') {
            return [...prev.slice(0, -1), { ...last, content: accumulated }];
          }
          return [...prev, { role: 'assistant', content: accumulated, ts: Date.now() }];
        });
      },
      () => {
        setStreaming(false);
        // Check for proposed action
        const ga = detectProposedAction(accumulated);
        if (ga) setGhostAction(ga);
      },
    );
    stopRef.current = stop;
  }, [streaming]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send(input);
    }
  };

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>

      {/* System context banner */}
      <div style={{
        padding: '8px 20px', borderBottom: '1px solid #0D2244',
        background: '#040C1A', display: 'flex', alignItems: 'center', gap: 10,
        flexShrink: 0, flexWrap: 'wrap', gap: 12,
      }}>
        <span style={{ fontSize: 10, color: '#4A6A8A', textTransform: 'uppercase', letterSpacing: 0.5 }}>Context:</span>
        {[
          { label: 'Root Cause: order-svc', color: '#FF3B5C' },
          { label: 'Confidence: 94.2%',      color: '#00D4FF' },
          { label: '2 Critical Alerts',      color: '#FF3B5C' },
          { label: 'OPA: 100%',             color: '#00FF9F' },
          { label: 'claude-sonnet-4',        color: '#9B5DE5' },
        ].map(c => (
          <span key={c.label} style={{
            fontSize: 10, color: c.color, fontFamily: 'JetBrains Mono, monospace',
            background: `${c.color}15`, border: `1px solid ${c.color}33`,
            borderRadius: 4, padding: '1px 6px',
          }}>{c.label}</span>
        ))}
      </div>

      {/* Messages */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '16px 20px' }}>
        {messages.length === 0 && (
          <div style={{ textAlign: 'center', marginTop: 40, color: '#4A6A8A' }}>
            <div style={{ fontSize: 36, marginBottom: 12 }}>💬</div>
            <div style={{ fontSize: 14, marginBottom: 6, color: '#8899AA' }}>CCDT Co-Pilot</div>
            <div style={{ fontSize: 12 }}>Ask about incidents, root causes, or request remediation proposals</div>
            <div style={{ marginTop: 20, display: 'flex', flexWrap: 'wrap', gap: 8, justifyContent: 'center' }}>
              {QUICK_PROMPTS.map(p => (
                <button
                  key={p}
                  onClick={() => send(p)}
                  style={{
                    background: '#06111F', border: '1px solid #0D2244', borderRadius: 6,
                    color: '#8899AA', fontSize: 11, padding: '5px 10px', cursor: 'pointer',
                    maxWidth: 200, textAlign: 'center', transition: 'border-color 0.15s',
                  }}
                  onMouseEnter={e => (e.currentTarget.style.borderColor = '#00D4FF44')}
                  onMouseLeave={e => (e.currentTarget.style.borderColor = '#0D2244')}
                >
                  {p}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((msg, i) => (
          <div
            key={i}
            style={{
              display:       'flex',
              justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start',
              marginBottom:  14,
              animation:     'fadeSlideIn 0.2s ease',
            }}
          >
            {msg.role === 'assistant' && (
              <div style={{
                width: 28, height: 28, borderRadius: '50%', background: '#9B5DE522',
                border: '1px solid #9B5DE544', display: 'flex', alignItems: 'center',
                justifyContent: 'center', fontSize: 14, marginRight: 10, flexShrink: 0, marginTop: 2,
              }}>
                🧠
              </div>
            )}

            <div style={{
              maxWidth:     '75%',
              padding:      '12px 14px',
              borderRadius: msg.role === 'user' ? '12px 12px 4px 12px' : '4px 12px 12px 12px',
              background:   msg.role === 'user' ? '#00D4FF22' : '#06111F',
              border:       `1px solid ${msg.role === 'user' ? '#00D4FF44' : '#0D2244'}`,
              fontSize:     12,
              color:        '#C8D8E8',
              lineHeight:   1.6,
              fontFamily:   msg.role === 'user' ? 'Inter, sans-serif' : 'Inter, sans-serif',
            }}>
              {msg.role === 'assistant' ? renderMarkdown(msg.content) : msg.content}
              {/* Streaming cursor */}
              {streaming && msg.role === 'assistant' && i === messages.length - 1 && (
                <span style={{ display: 'inline-block', width: 8, height: 14, background: '#9B5DE5', marginLeft: 2, verticalAlign: 'text-bottom', animation: 'blink 0.8s infinite' }} />
              )}
            </div>
          </div>
        ))}

        {/* Ghost Preview banner */}
        {ghostAction && !streaming && (
          <div style={{
            margin: '8px 0', padding: '10px 14px',
            background: '#9B5DE511', border: '1px solid #9B5DE544', borderRadius: 8,
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            animation: 'fadeSlideIn 0.3s ease',
          }}>
            <div style={{ fontSize: 12, color: '#9B5DE5' }}>
              ⚡ Proposed action detected: <strong>{ghostAction.label}</strong>
            </div>
            <button
              onClick={() => setGhostAction(ghostAction)}
              style={{
                background: '#9B5DE5', border: 'none', borderRadius: 6,
                color: '#030810', fontSize: 11, fontWeight: 700, padding: '5px 12px', cursor: 'pointer',
              }}
            >
              👻 Ghost Preview
            </button>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Quick prompts (shown after first message) */}
      {messages.length > 0 && (
        <div style={{ padding: '8px 20px', borderTop: '1px solid #0D2244', display: 'flex', gap: 6, flexWrap: 'wrap', flexShrink: 0 }}>
          {QUICK_PROMPTS.slice(0, 3).map(p => (
            <button
              key={p}
              onClick={() => send(p)}
              disabled={streaming}
              style={{
                background: 'transparent', border: '1px solid #0D2244', borderRadius: 5,
                color: '#4A6A8A', fontSize: 10, padding: '3px 8px', cursor: 'pointer',
              }}
            >
              {p}
            </button>
          ))}
        </div>
      )}

      {/* Input */}
      <div style={{
        padding:     '12px 20px',
        borderTop:   '1px solid #0D2244',
        display:     'flex',
        gap:         10,
        alignItems:  'flex-end',
        flexShrink:  0,
        background:  '#040C1A',
      }}>
        <textarea
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask CCDT Co-Pilot… (Enter to send, Shift+Enter for newline)"
          rows={2}
          style={{
            flex:        1,
            background:  '#06111F',
            border:      '1px solid #0D2244',
            borderRadius: 8,
            color:       '#C8D8E8',
            fontSize:    13,
            padding:     '10px 12px',
            resize:      'none',
            outline:     'none',
            fontFamily:  'Inter, sans-serif',
            lineHeight:  1.5,
          }}
          onFocus={e => (e.currentTarget.style.borderColor = '#00D4FF44')}
          onBlur={e  => (e.currentTarget.style.borderColor = '#0D2244')}
        />
        <button
          onClick={() => send(input)}
          disabled={!input.trim() || streaming}
          style={{
            background:   input.trim() && !streaming ? '#00D4FF' : '#0D2244',
            border:       'none',
            borderRadius: 8,
            color:        input.trim() && !streaming ? '#030810' : '#4A6A8A',
            fontSize:     13,
            fontWeight:   700,
            padding:      '10px 18px',
            cursor:       input.trim() && !streaming ? 'pointer' : 'default',
            height:       56,
            flexShrink:   0,
            transition:   'background 0.2s',
          }}
        >
          {streaming ? '…' : '→'}
        </button>
      </div>

      {ghostAction && (
        <GhostPreviewModal action={ghostAction} onClose={() => setGhostAction(null)} />
      )}
    </div>
  );
};

export default CopilotTab;

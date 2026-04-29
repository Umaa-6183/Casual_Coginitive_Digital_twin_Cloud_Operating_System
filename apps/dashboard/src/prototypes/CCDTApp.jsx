import { useState, useEffect, useRef, useCallback } from "react";
import {
  Zap,
  Map,
  AlertCircle,
  Brain,
  Shield,
  CheckCircle,
  BarChart3,
  Globe,
  MessageSquare,
  Bell,
  Settings,
  Skull,
  Ghost,
  Hourglass,
  Timer,
  Folder,
  Key,
  Search,
  Info,
  Wrench
} from "lucide-react";
// ─── THEME & CONSTANTS ───────────────────────────────────────────────────────
const T = {
  bg: "#0b1220",
  bg1: "#111827",
  bg2: "#1f2937",
  bg3: "#374151",
  border: "#112240",
  borderGlow: "#00D4FF22",
  cyan: "#00D4FF",
  cyanDim: "#00D4FF66",
  cyanBright: "#60EFFF",
  green: "#00FF9F",
  greenDim: "#00FF9F44",
  red: "#FF3B5C",
  redDim: "#FF3B5C33",
  orange: "#FF8C00",
  orangeDim: "#FF8C0033",
  yellow: "#FFD60A",
  purple: "#9B5DE5",
  text: "#C8D8E8",
  textDim: "#6B8199",
  textBright: "#E8F4FF",
};

// ─── TOPOLOGY DATA ────────────────────────────────────────────────────────────
const INITIAL_NODES = [
  { id: "ingress", label: "Ingress", x: 400, y: 60, status: "healthy", layer: "network", cpu: 12, mem: 34 },
  { id: "api-gw", label: "API Gateway", x: 400, y: 170, status: "healthy", layer: "service", cpu: 28, mem: 51 },
  { id: "auth", label: "Auth Service", x: 200, y: 290, status: "warning", layer: "service", cpu: 67, mem: 72 },
  { id: "user-svc", label: "User Service", x: 400, y: 290, status: "healthy", layer: "service", cpu: 23, mem: 41 },
  { id: "order-svc", label: "Order Service", x: 600, y: 290, status: "critical", layer: "service", cpu: 94, mem: 88 },
  { id: "cache", label: "Redis Cache", x: 140, y: 420, status: "healthy", layer: "data", cpu: 8, mem: 62 },
  { id: "postgres", label: "PostgreSQL", x: 340, y: 420, status: "warning", layer: "data", cpu: 45, mem: 79 },
  { id: "kafka", label: "Kafka", x: 540, y: 420, status: "healthy", layer: "data", cpu: 31, mem: 55 },
  { id: "ml-svc", label: "ML Service", x: 660, y: 420, status: "healthy", layer: "service", cpu: 71, mem: 83 },
  { id: "ebpf-agent", label: "eBPF Agent", x: 760, y: 170, status: "healthy", layer: "system", cpu: 3, mem: 18 },
];

const EDGES = [
  { from: "ingress", to: "api-gw", type: "http" },
  { from: "api-gw", to: "auth", type: "grpc" },
  { from: "api-gw", to: "user-svc", type: "grpc" },
  { from: "api-gw", to: "order-svc", type: "grpc" },
  { from: "auth", to: "cache", type: "tcp" },
  { from: "user-svc", to: "postgres", type: "tcp" },
  { from: "order-svc", to: "postgres", type: "tcp", causal: true },
  { from: "order-svc", to: "kafka", type: "tcp" },
  { from: "order-svc", to: "ml-svc", type: "grpc" },
  { from: "ebpf-agent", to: "api-gw", type: "probe" },
  { from: "ebpf-agent", to: "order-svc", type: "probe", causal: true },
];

const ALERTS = [
  { id: 1, time: "14:32:07", sev: "critical", msg: "Privilege escalation detected in order-svc container", node: "order-svc", type: "attack" },
  { id: 2, time: "14:31:55", sev: "warning", msg: "Memory pressure: postgres OOM risk at 79% usage", node: "postgres", type: "fault" },
  { id: 3, time: "14:31:22", sev: "warning", msg: "Auth service CPU spike: 67% — possible brute-force", node: "auth", type: "attack" },
  { id: 4, time: "14:30:45", sev: "info", msg: "TCP retransmit rate elevated on order-svc → postgres", node: "order-svc", type: "fault" },
  { id: 5, time: "14:29:18", sev: "info", msg: "Scheduler latency p99 increased by 12ms on node-3", node: "ingress", type: "fault" },
];

// ─── UTILITY FUNCTIONS ────────────────────────────────────────────────────────
const statusColor = (s) => ({
  healthy: T.green, warning: T.yellow, critical: T.red, unknown: T.textDim
}[s] || T.textDim);

const sevColor = (s) => ({
  critical: T.red, warning: T.yellow, info: T.cyan
}[s] || T.textDim);

const layerColor = (l) => ({
  network: T.cyan, service: T.purple, data: T.orange, system: T.green
}[l] || T.textDim);

const pulse = `
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
@keyframes scanline { 0%{transform:translateY(-100%)} 100%{transform:translateY(100vh)} }
@keyframes glow { 0%,100%{box-shadow:0 0 8px #00D4FF44} 50%{box-shadow:0 0 20px #00D4FFAA,0 0 40px #00D4FF44} }
@keyframes fadeIn { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:translateY(0)} }
@keyframes spin { to{transform:rotate(360deg)} }
@keyframes blink { 0%,100%{opacity:1} 49%{opacity:1} 50%{opacity:0} }
@keyframes dataflow { 0%{stroke-dashoffset:20} 100%{stroke-dashoffset:0} }
@keyframes ripple { 0%{r:4;opacity:0.8} 100%{r:16;opacity:0} }
`;

// ─── SUB-COMPONENTS ───────────────────────────────────────────────────────────

function GlowBadge({ label, color = T.cyan }) {
  return (
    <span style={{
      fontSize: 10, fontFamily: "'JetBrains Mono',monospace", letterSpacing: 1,
      padding: "2px 8px", borderRadius: 3, border: `1px solid ${color}55`,
      background: `${color}11`, color, textTransform: "uppercase",
    }}>{label}</span>
  );
}

function MetricBar({ value, max = 100, color }) {
  const pct = Math.min(100, (value / max) * 100);
  const col = pct > 85 ? T.red : pct > 65 ? T.yellow : color || T.cyan;
  return (
    <div style={{ height: 3, background: "#ffffff11", borderRadius: 2, overflow: "hidden", marginTop: 4 }}>
      <div style={{ width: `${pct}%`, height: "100%", background: col, borderRadius: 2, transition: "width 1s ease", boxShadow: `0 0 6px ${col}88` }} />
    </div>
  );
}

function NodeCard({ node, selected, onClick }) {
  const col = statusColor(node.status);
  const lCol = layerColor(node.layer);
  const isSelected = selected?.id === node.id;
  return (
    <div onClick={() => onClick(node)} style={{
      padding: "10px 14px", borderRadius: 6, cursor: "pointer", marginBottom: 6,
      background: isSelected ? `${T.cyan}11` : T.bg2,
      border: `1px solid ${isSelected ? T.cyan : T.border}`,
      boxShadow: isSelected ? `0 0 12px ${T.cyanDim}` : "none",
      transition: "all 0.2s",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ color: T.textBright, fontFamily: "'JetBrains Mono',monospace", fontSize: 12 }}>{node.label}</span>
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: col, boxShadow: `0 0 6px ${col}`, display: "inline-block", animation: node.status !== "healthy" ? "pulse 1.5s infinite" : "none" }} />
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
        <GlowBadge label={node.layer} color={lCol} />
        <GlowBadge label={node.status} color={col} />
      </div>
      <div style={{ marginTop: 8, fontSize: 11, color: T.textDim, fontFamily: "'JetBrains Mono',monospace" }}>
        <div style={{ display: "flex", justifyContent: "space-between" }}>
          <span>CPU {node.cpu}%</span><span>MEM {node.mem}%</span>
        </div>
        <MetricBar value={node.cpu} />
      </div>
    </div>
  );
}

// ─── TOPOLOGY MAP ─────────────────────────────────────────────────────────────
function TopologyMap({ nodes, selectedNode, onSelect, ghostTarget }) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const i = setInterval(() => setTick(t => t + 1), 1200);
    return () => clearInterval(i);
  }, []);

  const W = 820, H = 520;

  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ background: "transparent", display: "block" }}>
      <defs>
        <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
          <path d="M0,0 L0,6 L8,3 z" fill={T.cyanDim} />
        </marker>
        <marker id="arrow-causal" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
          <path d="M0,0 L0,6 L8,3 z" fill={T.red} />
        </marker>
        <filter id="glow-filter">
          <feGaussianBlur stdDeviation="3" result="blur" />
          <feComposite in="SourceGraphic" in2="blur" operator="over" />
        </filter>
        <radialGradient id="bg-grad" cx="50%" cy="50%" r="60%">
          <stop offset="0%" stopColor="#0B1E3A" />
          <stop offset="100%" stopColor="#030810" />
        </radialGradient>
      </defs>

      {/* Grid background */}
      <rect width={W} height={H} fill="url(#bg-grad)" rx="8" />
      {Array.from({ length: 16 }).map((_, i) => (
        <line key={`vg${i}`} x1={i * 55} y1={0} x2={i * 55} y2={H} stroke="#FFFFFF06" strokeWidth={0.5} />
      ))}
      {Array.from({ length: 10 }).map((_, i) => (
        <line key={`hg${i}`} x1={0} y1={i * 55} x2={W} y2={i * 55} stroke="#FFFFFF06" strokeWidth={0.5} />
      ))}

      {/* Edges */}
      {EDGES.map((e, i) => {
        const from = nodes.find(n => n.id === e.from);
        const to = nodes.find(n => n.id === e.to);
        if (!from || !to) return null;
        const isCausal = e.causal;
        const isProbe = e.type === "probe";
        const col = isCausal ? T.red : isProbe ? T.green : T.cyanDim;
        return (
          <g key={i}>
            <line
              x1={from.x} y1={from.y} x2={to.x} y2={to.y}
              stroke={col} strokeWidth={isCausal ? 2 : 1}
              strokeDasharray={isProbe ? "4 3" : isCausal ? "6 3" : "none"}
              strokeDashoffset={tick * (isCausal ? -2 : -1)}
              markerEnd={isCausal ? "url(#arrow-causal)" : "url(#arrow)"}
              opacity={isCausal ? 0.9 : 0.5}
              style={{ transition: "stroke-dashoffset 0.3s" }}
            />
            {isCausal && (
              <line x1={from.x} y1={from.y} x2={to.x} y2={to.y}
                stroke={T.red} strokeWidth={6} opacity={0.08} />
            )}
          </g>
        );
      })}

      {/* Nodes */}
      {nodes.map((node) => {
        const col = statusColor(node.status);
        const lCol = layerColor(node.layer);
        const isSel = selectedNode?.id === node.id;
        const isGhost = ghostTarget === node.id;
        const r = 28;
        return (
          <g key={node.id} onClick={() => onSelect(node)} style={{ cursor: "pointer" }}>
            {/* Ghost preview ring */}
            {isGhost && (
              <circle cx={node.x} cy={node.y} r={r + 12} fill="none"
                stroke={T.purple} strokeWidth={2} strokeDasharray="6 3"
                opacity={0.7} style={{ animation: "pulse 1s infinite" }} />
            )}
            {/* Selection glow */}
            {isSel && (
              <circle cx={node.x} cy={node.y} r={r + 8} fill="none"
                stroke={T.cyan} strokeWidth={1.5} opacity={0.5} />
            )}
            {/* Ripple for critical */}
            {node.status === "critical" && (
              <circle cx={node.x} cy={node.y} r={r + 4} fill="none"
                stroke={T.red} strokeWidth={1.5} opacity={0.3}
                style={{ animation: "pulse 1.2s infinite" }} />
            )}
            {/* Node background */}
            <circle cx={node.x} cy={node.y} r={r} fill={T.bg2}
              stroke={isSel ? T.cyan : col} strokeWidth={isSel ? 2 : 1.5}
              filter={isSel ? "url(#glow-filter)" : undefined} />
            {/* Layer accent */}
            <circle cx={node.x} cy={node.y} r={r - 4} fill={`${lCol}08`} stroke={`${lCol}22`} strokeWidth={1} />
            {/* Status dot */}
            <circle cx={node.x + 18} cy={node.y - 18} r={5} fill={col}
              stroke={T.bg} strokeWidth={1.5}
              style={{ animation: node.status !== "healthy" ? "pulse 1.5s infinite" : "none" }} />
            {/* Label */}
            <text x={node.x} y={node.y + 4} textAnchor="middle" dominantBaseline="middle"
              fill={T.textBright} fontSize={9} fontFamily="'JetBrains Mono',monospace"
              fontWeight="600" letterSpacing={0.5}>
              {node.label.split(" ").map((word, i, arr) => (
                <tspan key={i} x={node.x} dy={arr.length > 1 ? (i === 0 ? -5 : 13) : 0}>{word}</tspan>
              ))}
            </text>
            {/* CPU mini-bar */}
            <rect x={node.x - 18} y={node.y + 22} width={36} height={3} rx={1} fill="#FFFFFF11" />
            <rect x={node.x - 18} y={node.y + 22}
              width={Math.max(1, 36 * node.cpu / 100)} height={3} rx={1}
              fill={node.cpu > 85 ? T.red : node.cpu > 65 ? T.yellow : T.cyan}
              opacity={0.8} />
          </g>
        );
      })}

      {/* Legend */}
      <g transform={`translate(16, ${H - 60})`}>
        {[
          { col: T.cyanDim, label: "Network Flow" },
          { col: T.red, label: "Causal Chain" },
          { col: T.green, label: "eBPF Probe" },
        ].map((l, i) => (
          <g key={i} transform={`translate(0, ${i * 16})`}>
            <line x1={0} y1={6} x2={20} y2={6} stroke={l.col} strokeWidth={1.5} />
            <text x={26} y={10} fill={T.textDim} fontSize={10} fontFamily="'JetBrains Mono',monospace">{l.label}</text>
          </g>
        ))}
      </g>
    </svg>
  );
}

// ─── GHOST PREVIEW MODAL ──────────────────────────────────────────────────────
function GhostPreviewModal({ action, onApprove, onReject }) {
  const [step, setStep] = useState(0);
  const [simResult, setSimResult] = useState(null);

  useEffect(() => {
    setStep(0);
    setSimResult(null);
    const t1 = setTimeout(() => setStep(1), 800);
    const t2 = setTimeout(() => setStep(2), 2200);
    const t3 = setTimeout(() => {
      setStep(3);
      setSimResult({
        mttr: "-68%", affected: 2, collateral: 0, opaStatus: "PASS",
        riskScore: 12, projectedState: "STABLE", trafficImpact: "< 2%",
        confidence: 94,
      });
    }, 3800);
    return () => { clearTimeout(t1); clearTimeout(t2); clearTimeout(t3); };
  }, [action]);

  const steps = [
    "Initializing Ghost Preview environment…",
    "Cloning current cluster state snapshot…",
    "Simulating: " + (action?.label || "action") + "…",
    "Validating against OPA safety policies…",
  ];

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 1000,
      background: "#000000CC", backdropFilter: "blur(8px)",
      display: "flex", alignItems: "center", justifyContent: "center",
    }}>
      <div style={{
        width: 640, background: T.bg1, border: `1px solid ${T.purple}`,
        borderRadius: 12, overflow: "hidden",
        boxShadow: `0 0 40px ${T.purple}44, 0 0 80px #00000088`,
        animation: "fadeIn 0.3s ease",
      }}>
        {/* Header */}
        <div style={{
          padding: "16px 24px", borderBottom: `1px solid ${T.border}`,
          background: `${T.purple}11`,
          display: "flex", justifyContent: "space-between", alignItems: "center",
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ fontSize: 18 }}>👻</span>
            <div>
              <div style={{ color: T.purple, fontFamily: "'JetBrains Mono',monospace", fontSize: 13, fontWeight: 700, letterSpacing: 1 }}>
                GHOST PREVIEW SIMULATOR
              </div>
              <div style={{ color: T.textDim, fontSize: 11, marginTop: 2 }}>Safety-validated action simulation</div>
            </div>
          </div>
          <GlowBadge label="OPA ENFORCED" color={T.purple} />
        </div>

        {/* Action being simulated */}
        <div style={{ padding: "16px 24px", borderBottom: `1px solid ${T.border}` }}>
          <div style={{ color: T.textDim, fontSize: 11, fontFamily: "'JetBrains Mono',monospace", marginBottom: 6 }}>PROPOSED ACTION</div>
          <div style={{
            padding: "10px 14px", background: T.bg2, borderRadius: 6,
            border: `1px solid ${T.border}`, color: T.textBright,
            fontFamily: "'JetBrains Mono',monospace", fontSize: 13,
          }}>
            {action?.icon} {action?.label}
          </div>
        </div>

        {/* Simulation steps */}
        <div style={{ padding: "16px 24px" }}>
          <div style={{ color: T.textDim, fontSize: 11, fontFamily: "'JetBrains Mono',monospace", marginBottom: 10 }}>SIMULATION PROGRESS</div>
          {steps.map((s, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8, opacity: i <= step ? 1 : 0.3, transition: "opacity 0.5s" }}>
              <div style={{
                width: 18, height: 18, borderRadius: "50%", flexShrink: 0,
                background: i < step ? T.green : i === step ? T.cyan : T.bg3,
                border: `1.5px solid ${i < step ? T.green : i === step ? T.cyan : T.border}`,
                display: "flex", alignItems: "center", justifyContent: "center",
                fontSize: 10, color: T.bg,
                boxShadow: i === step ? `0 0 8px ${T.cyan}` : "none",
              }}>
                {i < step ? "✓" : i === step ? <span style={{ animation: "spin 1s linear infinite", display: "inline-block" }}>◌</span> : ""}
              </div>
              <span style={{ fontSize: 12, color: i < step ? T.green : i === step ? T.cyan : T.textDim, fontFamily: "'JetBrains Mono',monospace" }}>
                {s}
              </span>
            </div>
          ))}
        </div>

        {/* Results */}
        {simResult && (
          <div style={{ padding: "0 24px 16px", animation: "fadeIn 0.4s ease" }}>
            <div style={{ color: T.textDim, fontSize: 11, fontFamily: "'JetBrains Mono',monospace", marginBottom: 10 }}>SIMULATION RESULTS</div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, marginBottom: 14 }}>
              {[
                { label: "MTTR Impact", value: simResult.mttr, color: T.green },
                { label: "OPA Check", value: simResult.opaStatus, color: T.green },
                { label: "Risk Score", value: `${simResult.riskScore}/100`, color: T.green },
                { label: "Traffic Impact", value: simResult.trafficImpact, color: T.yellow },
                { label: "Projected State", value: simResult.projectedState, color: T.cyan },
                { label: "Confidence", value: `${simResult.confidence}%`, color: T.cyan },
              ].map((m, i) => (
                <div key={i} style={{ padding: "10px 12px", background: T.bg2, borderRadius: 6, border: `1px solid ${T.border}` }}>
                  <div style={{ color: T.textDim, fontSize: 10, fontFamily: "'JetBrains Mono',monospace", marginBottom: 4 }}>{m.label}</div>
                  <div style={{ color: m.color, fontSize: 14, fontFamily: "'JetBrains Mono',monospace", fontWeight: 700 }}>{m.value}</div>
                </div>
              ))}
            </div>
            <div style={{
              padding: "10px 14px", background: `${T.green}11`, border: `1px solid ${T.green}44`,
              borderRadius: 6, color: T.green, fontSize: 12, fontFamily: "'JetBrains Mono',monospace",
              marginBottom: 14,
            }}>
              ✓ All OPA safety constraints satisfied. Action is safe to execute.
            </div>
            <div style={{ display: "flex", gap: 10 }}>
              <button onClick={onApprove} style={{
                flex: 1, padding: "12px 0", background: `${T.green}22`,
                border: `1px solid ${T.green}`, borderRadius: 6, color: T.green,
                fontFamily: "'JetBrains Mono',monospace", fontSize: 12, fontWeight: 700,
                cursor: "pointer", letterSpacing: 1, transition: "all 0.2s",
              }}>▶ APPROVE & EXECUTE</button>
              <button onClick={onReject} style={{
                flex: 1, padding: "12px 0", background: `${T.red}11`,
                border: `1px solid ${T.red}44`, borderRadius: 6, color: T.red,
                fontFamily: "'JetBrains Mono',monospace", fontSize: 12, cursor: "pointer",
                letterSpacing: 1, transition: "all 0.2s",
              }}>✕ CANCEL</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── COPILOT CHAT ─────────────────────────────────────────────────────────────
function CoPilotChat({ nodes, alerts }) {
  const [messages, setMessages] = useState([
    {
      role: "assistant",
      content: "👋 **CCDT Co-Pilot online.** I have full observability into your cluster — eBPF sensors, causal graph, and guardian layer are all active.\n\nI'm currently tracking **2 critical incidents** and **2 warnings**. How can I help you investigate?",
    }
  ]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [ghostAction, setGhostAction] = useState(null);
  const bottomRef = useRef(null);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  const systemPrompt = `You are CCDT Co-Pilot, an advanced AIOps security assistant for a Kubernetes cluster running a Cognitive Digital Twin system. You have real-time access to eBPF sensor data, Causal Graph Neural Networks, and the Guardian Layer.

Current cluster state:
${nodes.map(n => `- ${n.label} (${n.layer}): status=${n.status}, CPU=${n.cpu}%, MEM=${n.mem}%`).join("\n")}

Active alerts:
${alerts.map(a => `- [${a.sev.toUpperCase()}] ${a.msg} (type: ${a.type})`).join("\n")}

You can:
1. Analyze incidents using causal reasoning (not just correlation)
2. Distinguish cyber-attacks from performance faults
3. Propose remediation actions (these will be validated by Ghost Preview + OPA before execution)
4. Answer counterfactual "what if" queries
5. Provide natural language incident summaries

Keep responses concise, technical, and actionable. Use markdown formatting. When proposing actions, prefix them with "PROPOSED ACTION:".`;

  const quickPrompts = [
    "Is the order-svc issue an attack or a fault?",
    "What caused the postgres degradation?",
    "What if we isolate the order-svc container?",
    "Summarize all active incidents",
    "What's the blast radius of the critical alert?",
  ];

  const sendMessage = useCallback(async (text) => {
    const userMsg = text || input.trim();
    if (!userMsg) return;
    setInput("");
    const newMessages = [...messages, { role: "user", content: userMsg }];
    setMessages(newMessages);
    setLoading(true);

    try {
      const res = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "claude-sonnet-4-20250514",
          max_tokens: 1000,
          system: systemPrompt,
          messages: newMessages.map(m => ({ role: m.role, content: m.content })),
        }),
      });
      const data = await res.json();
      const reply = data.content?.[0]?.text || "Error receiving response.";
      setMessages(prev => [...prev, { role: "assistant", content: reply }]);

      // Check if the response proposes an action for Ghost Preview
      if (reply.includes("PROPOSED ACTION:") || reply.toLowerCase().includes("isolate") || reply.toLowerCase().includes("block ip")) {
        setGhostAction({
          label: "Isolate order-svc container & block suspicious IP 10.0.0.47",
          icon: <Shield size={16} />,
        });
      }
    } catch (err) {
      setMessages(prev => [...prev, { role: "assistant", content: "⚠️ Connection error. Retrying…" }]);
    } finally {
      setLoading(false);
    }
  }, [messages, input, systemPrompt]);

  const formatMsg = (text) => {
    return text
      .replace(/\*\*(.*?)\*\*/g, `<strong style="color:${T.cyanBright}">$1</strong>`)
      .replace(/`(.*?)`/g, `<code style="background:${T.bg3};padding:1px 5px;border-radius:3px;font-size:11px;color:${T.orange}">${"$1"}</code>`)
      .replace(/\n/g, "<br/>");
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {ghostAction && (
        <div style={{
          padding: "10px 16px", background: `${T.purple}11`,
          border: `1px solid ${T.purple}44`, borderRadius: 6, margin: "0 0 12px",
          display: "flex", justifyContent: "space-between", alignItems: "center",
        }}>
          <div>
            <span style={{ color: T.purple, fontSize: 11, fontFamily: "'JetBrains Mono',monospace" }}>👻 PROPOSED ACTION READY FOR GHOST PREVIEW</span>
            <div style={{ color: T.textDim, fontSize: 11, marginTop: 2 }}>{ghostAction.label}</div>
          </div>
          <button onClick={() => setGhostAction(null)} style={{
            padding: "6px 14px", background: `${T.purple}22`, border: `1px solid ${T.purple}`,
            borderRadius: 4, color: T.purple, fontFamily: "'JetBrains Mono',monospace",
            fontSize: 11, cursor: "pointer",
          }}>RUN PREVIEW →</button>
        </div>
      )}

      {/* Messages */}
      <div style={{ flex: 1, overflowY: "auto", paddingRight: 4, marginBottom: 12 }}>
        {messages.map((m, i) => (
          <div key={i} style={{
            display: "flex", gap: 10, marginBottom: 16,
            justifyContent: m.role === "user" ? "flex-end" : "flex-start",
            animation: "fadeIn 0.3s ease",
          }}>
            {m.role === "assistant" && (
              <div style={{
                width: 30, height: 30, borderRadius: "50%", flexShrink: 0,
                background: `${T.cyan}22`, border: `1px solid ${T.cyanDim}`,
                display: "flex", alignItems: "center", justifyContent: "center",
                fontSize: 14, marginTop: 2,
              }}>🤖</div>
            )}
            <div style={{
              maxWidth: "78%", padding: "12px 16px", borderRadius: m.role === "user" ? "12px 12px 4px 12px" : "12px 12px 12px 4px",
              background: m.role === "user" ? `${T.cyan}22` : T.bg2,
              border: `1px solid ${m.role === "user" ? T.cyanDim : T.border}`,
              color: T.text, fontSize: 13, lineHeight: 1.6,
              fontFamily: m.role === "user" ? "'JetBrains Mono',monospace" : "system-ui,sans-serif",
            }} dangerouslySetInnerHTML={{ __html: formatMsg(m.content) }} />
          </div>
        ))}

        {loading && (
          <div style={{ display: "flex", gap: 10, marginBottom: 16 }}>
            <div style={{ width: 30, height: 30, borderRadius: "50%", background: `${T.cyan}22`, border: `1px solid ${T.cyanDim}`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14 }}>🤖</div>
            <div style={{ padding: "12px 16px", background: T.bg2, border: `1px solid ${T.border}`, borderRadius: "12px 12px 12px 4px" }}>
              <span style={{ color: T.cyanDim, fontFamily: "'JetBrains Mono',monospace", fontSize: 13 }}>
                analyzing causal graph<span style={{ animation: "blink 1s infinite" }}>_</span>
              </span>
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Quick prompts */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 10 }}>
        {quickPrompts.map((p, i) => (
          <button key={i} onClick={() => sendMessage(p)} style={{
            padding: "5px 10px", background: T.bg2, border: `1px solid ${T.border}`,
            borderRadius: 4, color: T.textDim, fontSize: 11,
            fontFamily: "'JetBrains Mono',monospace", cursor: "pointer",
            transition: "all 0.15s",
          }}
            onMouseEnter={e => { e.target.style.borderColor = T.cyan; e.target.style.color = T.cyan; }}
            onMouseLeave={e => { e.target.style.borderColor = T.border; e.target.style.color = T.textDim; }}>
            {p}
          </button>
        ))}
      </div>

      {/* Input */}
      <div style={{ display: "flex", gap: 8 }}>
        <input
          value={input} onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === "Enter" && !e.shiftKey && sendMessage()}
          placeholder="Ask the Co-Pilot… e.g. 'What caused this alert?'"
          style={{
            flex: 1, padding: "12px 16px", background: T.bg2,
            border: `1px solid ${T.border}`, borderRadius: 8,
            color: T.textBright, fontSize: 13, outline: "none",
            fontFamily: "'JetBrains Mono',monospace",
            transition: "border-color 0.2s",
          }}
          onFocus={e => e.target.style.borderColor = T.cyan}
          onBlur={e => e.target.style.borderColor = T.border}
        />
        <button onClick={() => sendMessage()} disabled={loading || !input.trim()} style={{
          padding: "12px 20px", background: loading ? T.bg3 : `${T.cyan}22`,
          border: `1px solid ${loading ? T.border : T.cyan}`,
          borderRadius: 8, color: loading ? T.textDim : T.cyan,
          fontFamily: "'JetBrains Mono',monospace", fontSize: 13,
          cursor: loading ? "not-allowed" : "pointer", fontWeight: 700,
          transition: "all 0.2s",
        }}>▶</button>
      </div>
    </div>
  );
}

// ─── GUARDIAN LAYER ───────────────────────────────────────────────────────────
function GuardianPanel({ onGhostPreview }) {
  const policies = [
    { id: "P001", name: "No container privilege escalation", status: "active", violations: 1 },
    { id: "P002", name: "Max pod CPU 95% threshold", status: "active", violations: 0 },
    { id: "P003", name: "Egress to unknown IPs requires approval", status: "active", violations: 1 },
    { id: "P004", name: "No lateral movement between namespaces", status: "active", violations: 0 },
    { id: "P005", name: "Memory OOM kill requires SRE notification", status: "active", violations: 0 },
  ];

  const rlActions = [
    { action: "Isolate order-svc container", confidence: 94, risk: "LOW", impact: "2% traffic" },
    { action: "Block IP 10.0.0.47 (suspicious)", confidence: 88, risk: "LOW", impact: "0% services" },
    { action: "Restart postgres with new limits", confidence: 71, risk: "MED", impact: "~30s downtime" },
    { action: "Scale auth-service replicas ×2", confidence: 82, risk: "LOW", impact: "Cost +$12/hr" },
  ];

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
      {/* OPA Policies */}
      <div
        style={{
          background: T.bg1,
          border: `1px solid ${T.border}`,
          borderRadius: 10,
          padding: 20,
        }}
      >
        <div
          style={{
            color: T.textDim,
            fontSize: 11,
            fontFamily: "'JetBrains Mono',monospace",
            letterSpacing: 1,
            marginBottom: 14,
          }}
        >
          OPA SAFETY POLICIES
        </div>
        {policies.map((p) => (
          <div
            key={p.id}
            style={{
              padding: "10px 12px",
              background: T.bg2,
              borderRadius: 6,
              border: `1px solid ${p.violations ? T.redDim : T.border}`,
              marginBottom: 8,
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            <div>
              <div style={{ color: T.textBright, fontSize: 12 }}>{p.name}</div>
              <div
                style={{
                  color: T.textDim,
                  fontSize: 10,
                  marginTop: 2,
                  fontFamily: "'JetBrains Mono',monospace",
                }}
              >
                {p.id}
              </div>
            </div>
            <div style={{ textAlign: "right" }}>
              {p.violations > 0 ? (
                <GlowBadge label={`${p.violations} VIOLATION`} color={T.red} />
              ) : (
                <GlowBadge label="PASS" color={T.green} />
              )}
            </div>
          </div>
        ))}
      </div>

      {/* RL Actions */}
      <div
        style={{
          background: T.bg1,
          border: `1px solid ${T.border}`,
          borderRadius: 10,
          padding: 20,
        }}
      >
        <div
          style={{
            color: T.textDim,
            fontSize: 11,
            fontFamily: "'JetBrains Mono',monospace",
            letterSpacing: 1,
            marginBottom: 14,
          }}
        >
          RL-PROPOSED REMEDIATION ACTIONS
        </div>
        {rlActions.map((a, i) => (
          <div
            key={i}
            style={{
              padding: "12px",
              background: T.bg2,
              borderRadius: 6,
              border: `1px solid ${T.border}`,
              marginBottom: 8,
            }}
          >
            <div style={{ color: T.textBright, fontSize: 12, marginBottom: 8 }}>
              {a.action}
            </div>
            <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>
              <GlowBadge label={`${a.confidence}% confidence`} color={T.cyan} />
              <GlowBadge
                label={`Risk: ${a.risk}`}
                color={a.risk === "LOW" ? T.green : T.yellow}
              />
            </div>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
              }}
            >
              <span style={{ color: T.textDim, fontSize: 11 }}>
                Impact: {a.impact}
              </span>
              <button
                onClick={() =>
                  onGhostPreview({
                    label: a.action,
                    icon: <Shield size={16} />,
                  })
                }
                style={{
                  padding: "5px 10px",
                  background: `${T.purple}22`,
                  border: `1px solid ${T.purple}55`,
                  borderRadius: 4,
                  color: T.purple,
                  fontSize: 11,
                  fontFamily: "'JetBrains Mono',monospace",
                  cursor: "pointer",
                }}
              >
                <Ghost size={16} /> Ghost Preview
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── INTELLIGENCE PANEL ───────────────────────────────────────────────────────
function IntelligencePanel() {
  const causalChain = [
    { step: 1, event: "Privilege escalation syscall in order-svc", type: "attack", time: "T+0ms" },
    { step: 2, event: "Unexpected capability change: CAP_SYS_ADMIN", type: "attack", time: "T+120ms" },
    { step: 3, event: "Unusual outbound TCP to 10.0.0.47:4444", type: "attack", time: "T+340ms" },
    { step: 4, event: "CPU spike: 94% (cryptominer signature detected)", type: "attack", time: "T+890ms" },
    { step: 5, event: "Cascading memory pressure on shared postgres", type: "fault", time: "T+1240ms" },
    { step: 6, event: "OOM risk propagating to user-svc reads", type: "fault", time: "T+1780ms" },
  ];

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1.2fr", gap: 16 }}>
      {/* GNN Metrics */}
      <div style={{ background: T.bg1, border: `1px solid ${T.border}`, borderRadius: 10, padding: 20 }}>
        <div style={{ color: T.textDim, fontSize: 11, fontFamily: "'JetBrains Mono',monospace", letterSpacing: 1, marginBottom: 14 }}>
          CAUSAL GNN — INFERENCE METRICS
        </div>
        {[
          { label: "Root Cause Confidence", value: "94.2%", sub: "order-svc privilege escalation", color: T.red },
          { label: "Attack vs Fault", value: "ATTACK", sub: "Cryptominer signature (p=0.94)", color: T.red },
          { label: "Blast Radius", value: "3 svcs", sub: "order-svc → postgres → user-svc", color: T.orange },
          { label: "Graph Nodes Analyzed", value: "10", sub: "Edges: 11 | DAG depth: 4", color: T.cyan },
          { label: "Counterfactual Ready", value: "YES", sub: "2 what-if scenarios available", color: T.green },
          { label: "MTTR Estimate", value: "4.2 min", sub: "If action taken now", color: T.green },
        ].map((m, i) => (
          <div key={i} style={{
            padding: "10px 14px", background: T.bg2, borderRadius: 6,
            border: `1px solid ${T.border}`, marginBottom: 8,
            display: "flex", justifyContent: "space-between", alignItems: "center",
          }}>
            <div>
              <div style={{ color: T.textDim, fontSize: 10, fontFamily: "'JetBrains Mono',monospace", marginBottom: 2 }}>{m.label}</div>
              <div style={{ color: T.textBright, fontSize: 11 }}>{m.sub}</div>
            </div>
            <div style={{ color: m.color, fontFamily: "'JetBrains Mono',monospace", fontSize: 16, fontWeight: 700 }}>{m.value}</div>
          </div>
        ))}
      </div>

      {/* Causal Chain */}
      <div style={{ background: T.bg1, border: `1px solid ${T.border}`, borderRadius: 10, padding: 20 }}>
        <div style={{ color: T.textDim, fontSize: 11, fontFamily: "'JetBrains Mono',monospace", letterSpacing: 1, marginBottom: 14 }}>
          CAUSAL CHAIN — ROOT CAUSE TRACE
        </div>
        <div style={{ position: "relative" }}>
          <div style={{ position: "absolute", left: 17, top: 0, bottom: 0, width: 2, background: `${T.border}`, borderRadius: 2 }} />
          {causalChain.map((c, i) => (
            <div key={i} style={{ display: "flex", gap: 14, marginBottom: 14, position: "relative", animation: `fadeIn 0.4s ease ${i * 0.1}s both` }}>
              <div style={{
                width: 34, height: 34, borderRadius: "50%", flexShrink: 0,
                background: c.type === "attack" ? T.redDim : T.orangeDim,
                border: `2px solid ${c.type === "attack" ? T.red : T.orange}`,
                display: "flex", alignItems: "center", justifyContent: "center",
                fontSize: 12, fontFamily: "'JetBrains Mono',monospace", fontWeight: 700,
                color: c.type === "attack" ? T.red : T.orange, zIndex: 1,
              }}>{c.step}</div>
              <div style={{ paddingTop: 4 }}>
                <div style={{ color: T.textBright, fontSize: 12, lineHeight: 1.4 }}>{c.event}</div>
                <div style={{ display: "flex", gap: 8, marginTop: 4 }}>
                  <GlowBadge label={c.type} color={c.type === "attack" ? T.red : T.orange} />
                  <span style={{ color: T.textDim, fontSize: 10, fontFamily: "'JetBrains Mono',monospace" }}>{c.time}</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ─── VALIDATION PANEL ─────────────────────────────────────────────────────────
function ValidationPanel() {
  const kpis = [
    { label: "MTTR Reduction", value: "68%", sub: "vs 6-month baseline", color: T.green },
    { label: "False Positive Rate", value: "2.1%", sub: "Down from 34%", color: T.green },
    { label: "Safety Compliance", value: "100%", sub: "All OPA policies passed", color: T.green },
    { label: "Explainability", value: "94.2%", sub: "Human-validated accuracy", color: T.cyan },
    { label: "Incidents Resolved", value: "47", sub: "Autonomous (last 7d)", color: T.purple },
    { label: "Avg Response Time", value: "1.8s", sub: "eBPF → Action", color: T.cyan },
  ];

  return (
    <div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12, marginBottom: 16 }}>
        {kpis.map((k, i) => (
          <div key={i} style={{
            padding: "20px", background: T.bg1, borderRadius: 10,
            border: `1px solid ${T.border}`,
            boxShadow: `inset 0 0 20px ${k.color}08`,
          }}>
            <div style={{ color: T.textDim, fontSize: 11, fontFamily: "'JetBrains Mono',monospace", letterSpacing: 1, marginBottom: 8 }}>{k.label}</div>
            <div style={{ color: k.color, fontSize: 36, fontFamily: "'JetBrains Mono',monospace", fontWeight: 700, lineHeight: 1 }}>{k.value}</div>
            <div style={{ color: T.textDim, fontSize: 11, marginTop: 6 }}>{k.sub}</div>
          </div>
        ))}
      </div>

      <div style={{ background: T.bg1, border: `1px solid ${T.border}`, borderRadius: 10, padding: 20 }}>
        <div style={{ color: T.textDim, fontSize: 11, fontFamily: "'JetBrains Mono',monospace", letterSpacing: 1, marginBottom: 14 }}>
          PHASE COMPLETION STATUS
        </div>
        {[
          { phase: "Phase 1: Digital Shadow", pct: 100, status: "COMPLETE" },
          { phase: "Phase 2: Data Modeling", pct: 100, status: "COMPLETE" },
          { phase: "Phase 3: Intelligence Layer", pct: 87, status: "ACTIVE" },
          { phase: "Phase 4: Autonomous Control", pct: 62, status: "ACTIVE" },
          { phase: "Phase 5: Human Interface", pct: 45, status: "ACTIVE" },
          { phase: "Phase 6: Validation", pct: 28, status: "ONGOING" },
        ].map((p, i) => (
          <div key={i} style={{ marginBottom: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
              <span style={{ color: T.textBright, fontSize: 12 }}>{p.phase}</span>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <GlowBadge label={p.status} color={p.status === "COMPLETE" ? T.green : p.status === "ACTIVE" ? T.cyan : T.yellow} />
                <span style={{ color: T.textDim, fontSize: 11, fontFamily: "'JetBrains Mono',monospace" }}>{p.pct}%</span>
              </div>
            </div>
            <MetricBar value={p.pct} color={p.status === "COMPLETE" ? T.green : T.cyan} />
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── MAIN APP ─────────────────────────────────────────────────────────────────
export default function CCDTApp() {
  const [activeTab, setActiveTab] = useState("topology");
  const [nodes, setNodes] = useState(INITIAL_NODES);
  const [selectedNode, setSelectedNode] = useState(null);
  const [ghostModal, setGhostModal] = useState(null);
  const [clock, setClock] = useState(new Date());
  const [toast, setToast] = useState(null);

  useEffect(() => {
    const i = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(i);
  }, []);

  // Simulate live data
  useEffect(() => {
    const i = setInterval(() => {
      setNodes(prev => prev.map(n => ({
        ...n,
        cpu: Math.max(1, Math.min(99, n.cpu + (Math.random() - 0.48) * 4)),
        mem: Math.max(10, Math.min(99, n.mem + (Math.random() - 0.49) * 2)),
      })));
    }, 2000);
    return () => clearInterval(i);
  }, []);

  const showToast = (msg, color = T.green) => {
    setToast({ msg, color });
    setTimeout(() => setToast(null), 3500);
  };

  const nav = [
    { id: "topology", label: "Topology", icon: <Map size={16} /> },
    { id: "intelligence", label: "Intelligence", icon: <Brain size={16} /> },
    { id: "guardian", label: "Guardian", icon: <Shield size={16} /> },
    { id: "copilot", label: "Co-Pilot", icon: <MessageSquare size={16} /> },
    { id: "validation", label: "Validation", icon: <BarChart3 size={16} /> },
  ];

  const systemStatus = nodes.some(n => n.status === "critical") ? "critical"
    : nodes.some(n => n.status === "warning") ? "warning" : "healthy";

  return (
    <div
      style={{
        background: T.bg,
        minHeight: "100vh",
        color: T.text,
        fontFamily: "system-ui, -apple-system, sans-serif",
      }}
    >
      <style>
        {pulse}
        {`
          * { box-sizing: border-box; margin: 0; padding: 0; }
          ::-webkit-scrollbar { width: 4px; } 
          ::-webkit-scrollbar-track { background: ${T.bg}; }
          ::-webkit-scrollbar-thumb { background: ${T.border}; border-radius: 2px; }
          @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Rajdhani:wght@500;600;700&display=swap');
        `}
      </style>

      {/* Toast */}
      {toast && (
        <div
          style={{
            position: "fixed",
            top: 20,
            right: 20,
            zIndex: 2000,
            padding: "12px 20px",
            background: T.bg1,
            border: `1px solid ${toast.color}`,
            borderRadius: 8,
            color: toast.color,
            fontFamily: "'JetBrains Mono',monospace",
            fontSize: 12,
            animation: "fadeIn 0.3s ease",
            boxShadow: `0 0 20px ${toast.color}44`,
          }}
        >
          {toast.msg}
        </div>
      )}

      {/* Ghost Preview Modal */}
      {ghostModal && (
        <GhostPreviewModal
          action={ghostModal}
          onApprove={() => {
            setGhostModal(null);
            showToast(`✓ Action executed: ${ghostModal.label}`);
            setNodes((prev) =>
              prev.map((n) =>
                n.id === "order-svc" ? { ...n, status: "healthy", cpu: 12 } : n,
              ),
            );
          }}
          onReject={() => setGhostModal(null)}
        />
      )}

      {/* Top Nav */}
      <div
        style={{
          height: 60,
          background: T.bg1,
          borderBottom: `1px solid ${T.border}`,
          display: "flex",
          alignItems: "center",
          padding: "0 24px",
          justifyContent: "space-between",
          position: "sticky",
          top: 0,
          zIndex: 100,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          {/* Logo */}
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div
              style={{
                width: 36,
                height: 36,
                borderRadius: 8,
                background: `linear-gradient(135deg, ${T.cyan}33, ${T.purple}33)`,
                border: `1px solid ${T.cyanDim}`,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 18,
              }}
            >
              ⬡
            </div>
            <div>
              <div
                style={{
                  color: T.cyanBright,
                  fontFamily: "'JetBrains Mono',monospace",
                  fontSize: 14,
                  fontWeight: 700,
                  letterSpacing: 1,
                }}
              >
                CCDT
              </div>
              <div style={{ color: T.textDim, fontSize: 10, letterSpacing: 1 }}>
                CLOUD COGNITIVE DIGITAL TWIN
              </div>
            </div>
          </div>

          {/* System status */}
          <div
            style={{
              padding: "4px 12px",
              borderRadius: 4,
              background:
                systemStatus === "critical"
                  ? T.redDim
                  : systemStatus === "warning"
                    ? T.orangeDim
                    : T.greenDim,
              border: `1px solid ${systemStatus === "critical" ? T.red : systemStatus === "warning" ? T.orange : T.green}44`,
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <span
              style={{
                width: 7,
                height: 7,
                borderRadius: "50%",
                background:
                  systemStatus === "critical"
                    ? T.red
                    : systemStatus === "warning"
                      ? T.orange
                      : T.green,
                display: "inline-block",
                animation:
                  systemStatus !== "healthy" ? "pulse 1.2s infinite" : "none",
              }}
            />
            <span
              style={{
                color:
                  systemStatus === "critical"
                    ? T.red
                    : systemStatus === "warning"
                      ? T.orange
                      : T.green,
                fontSize: 11,
                fontFamily: "'JetBrains Mono',monospace",
                fontWeight: 700,
              }}
            >
              SYSTEM {systemStatus.toUpperCase()}
            </span>
          </div>
        </div>

        {/* Right side */}
        <div style={{ display: "flex", alignItems: "center", gap: 20 }}>
          {[
            { label: "eBPF SENSORS", value: "10/10 ONLINE", color: T.green },
            { label: "GNN STATUS", value: "INFERENCE ACTIVE", color: T.cyan },
            { label: "GUARDIAN", value: "5 POLICIES ACTIVE", color: T.purple },
          ].map((s, i) => (
            <div key={i} style={{ textAlign: "right" }}>
              <div
                style={{
                  color: T.textDim,
                  fontSize: 9,
                  fontFamily: "'JetBrains Mono',monospace",
                  letterSpacing: 0.5,
                }}
              >
                {s.label}
              </div>
              <div
                style={{
                  color: s.color,
                  fontSize: 11,
                  fontFamily: "'JetBrains Mono',monospace",
                  fontWeight: 600,
                }}
              >
                {s.value}
              </div>
            </div>
          ))}
          <div
            style={{
              color: T.textDim,
              fontFamily: "'JetBrains Mono',monospace",
              fontSize: 12,
              borderLeft: `1px solid ${T.border}`,
              paddingLeft: 16,
            }}
          >
            {clock.toLocaleTimeString("en-US", { hour12: false })}
          </div>
        </div>
      </div>

      <div style={{ display: "flex", height: "calc(100vh - 60px)" }}>
        {/* Left Sidebar */}
        <div
          style={{
            width: 200,
            background: T.bg1,
            borderRight: `1px solid ${T.border}`,
            display: "flex",
            flexDirection: "column",
            padding: "16px 12px",
            flexShrink: 0,
          }}
        >
          {/* Navigation */}
          <div style={{ marginBottom: 20 }}>
            {nav.map((item) => (
              <button
                key={item.id}
                onClick={() => setActiveTab(item.id)}
                style={{
                  width: "100%",
                  padding: "10px 14px",
                  background:
                    activeTab === item.id ? `${T.cyan}18` : "transparent",
                  border: `1px solid ${activeTab === item.id ? T.cyanDim : "transparent"}`,
                  borderRadius: 6,
                  color: activeTab === item.id ? T.cyan : T.textDim,
                  fontFamily: "'JetBrains Mono',monospace",
                  fontSize: 12,
                  cursor: "pointer",
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  marginBottom: 4,
                  textAlign: "left",
                  transition: "all 0.15s",
                }}
              >
                <span>{item.icon}</span> {item.label}
              </button>
            ))}
          </div>

          <div
            style={{
              color: T.textDim,
              fontSize: 10,
              fontFamily: "'JetBrains Mono',monospace",
              letterSpacing: 1,
              marginBottom: 8,
              padding: "0 4px",
            }}
          >
            LIVE ALERTS
          </div>
          <div style={{ flex: 1, overflowY: "auto" }}>
            {ALERTS.map((a) => (
              <div
                key={a.id}
                style={{
                  padding: "8px 10px",
                  background: T.bg2,
                  borderRadius: 5,
                  border: `1px solid ${a.sev === "critical" ? T.redDim : T.border}`,
                  marginBottom: 6,
                }}
              >
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    marginBottom: 4,
                  }}
                >
                  <span
                    style={{
                      width: 7,
                      height: 7,
                      borderRadius: "50%",
                      background: sevColor(a.sev),
                      display: "inline-block",
                      marginTop: 3,
                      flexShrink: 0,
                    }}
                  />
                  <span
                    style={{
                      color: T.textDim,
                      fontSize: 9,
                      fontFamily: "'JetBrains Mono',monospace",
                    }}
                  >
                    {a.time}
                  </span>
                </div>
                <div
                  style={{ color: T.textBright, fontSize: 10, lineHeight: 1.4 }}
                >
                  {a.msg}
                </div>
                <div style={{ marginTop: 4 }}>
                  <GlowBadge
                    label={a.type}
                    color={a.type === "attack" ? T.red : T.orange}
                  />
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Main Content */}
        <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
          {/* TOPOLOGY TAB */}
          {activeTab === "topology" && (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 240px",
                gap: 16,
                height: "100%",
              }}
            >
              <div
                style={{ display: "flex", flexDirection: "column", gap: 16 }}
              >
                {/* Topology map */}
                <div
                  style={{
                    background: T.bg1,
                    border: `1px solid ${T.border}`,
                    borderRadius: 10,
                    overflow: "hidden",
                  }}
                >
                  <div
                    style={{
                      padding: "12px 16px",
                      borderBottom: `1px solid ${T.border}`,
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                    }}
                  >
                    <div>
                      <div
                        style={{
                          color: T.textBright,
                          fontFamily: "'JetBrains Mono',monospace",
                          fontSize: 13,
                          fontWeight: 700,
                        }}
                      >
                        REAL-TIME TOPOLOGY MAP
                      </div>
                      <div
                        style={{ color: T.textDim, fontSize: 11, marginTop: 2 }}
                      >
                        Directed Acyclic Graph — Causal chains highlighted
                      </div>
                    </div>
                    <div style={{ display: "flex", gap: 8 }}>
                      <GlowBadge label="LIVE" color={T.green} />
                      <GlowBadge
                        label={`${nodes.length} NODES`}
                        color={T.cyan}
                      />
                    </div>
                  </div>
                  <TopologyMap
                    nodes={nodes}
                    selectedNode={selectedNode}
                    onSelect={setSelectedNode}
                    ghostTarget={null}
                  />
                </div>

                {/* Layer health */}
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(4, 1fr)",
                    gap: 10,
                  }}
                >
                  {[
                    {
                      layer: "Layer 1",
                      name: "Nervous System",
                      sub: "eBPF Sensors",
                      icon: <Zap size={16} />,
                      color: T.green,
                      status: "ONLINE",
                      metrics: "847K events/s",
                    },
                    {
                      layer: "Layer 2",
                      name: "Cognitive Core",
                      sub: "Causal GNN",
                      icon: <Brain size={16} />,
                      color: T.cyan,
                      status: "ACTIVE",
                      metrics: "Inference: 4.2ms",
                    },
                    {
                      layer: "Layer 3",
                      name: "Guardian",
                      sub: "OPA + RL",
                      icon: <Shield size={16} />,
                      color: T.purple,
                      status: "ENFORCING",
                      metrics: "5 policies active",
                    },
                    {
                      layer: "Layer 4",
                      name: "Co-Pilot",
                      sub: "LLM Interface",
                      icon: <MessageSquare size={16} />,
                      color: T.orange,
                      status: "READY",
                      metrics: "claude-sonnet",
                    },
                  ].map((l, i) => (
                    <div
                      key={i}
                      style={{
                        padding: "14px",
                        background: T.bg1,
                        borderRadius: 8,
                        border: `1px solid ${l.color}33`,
                        boxShadow: `inset 0 0 20px ${l.color}08`,
                      }}
                    >
                      <div
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                          alignItems: "flex-start",
                          marginBottom: 8,
                        }}
                      >
                        <span style={{ fontSize: 20 }}>{l.icon}</span>
                        <GlowBadge label={l.status} color={l.color} />
                      </div>
                      <div
                        style={{
                          color: T.textDim,
                          fontSize: 9,
                          fontFamily: "'JetBrains Mono',monospace",
                          letterSpacing: 0.5,
                        }}
                      >
                        {l.layer}
                      </div>
                      <div
                        style={{
                          color: T.textBright,
                          fontSize: 13,
                          fontWeight: 600,
                          margin: "2px 0",
                        }}
                      >
                        {l.name}
                      </div>
                      <div style={{ color: T.textDim, fontSize: 10 }}>
                        {l.sub}
                      </div>
                      <div
                        style={{
                          color: l.color,
                          fontSize: 10,
                          fontFamily: "'JetBrains Mono',monospace",
                          marginTop: 6,
                        }}
                      >
                        {l.metrics}
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Node detail panel */}
              <div
                style={{ display: "flex", flexDirection: "column", gap: 12 }}
              >
                <div
                  style={{
                    background: T.bg1,
                    border: `1px solid ${T.border}`,
                    borderRadius: 10,
                    padding: 16,
                    flex: 1,
                    overflowY: "auto",
                  }}
                >
                  <div
                    style={{
                      color: T.textDim,
                      fontSize: 11,
                      fontFamily: "'JetBrains Mono',monospace",
                      letterSpacing: 1,
                      marginBottom: 12,
                    }}
                  >
                    SERVICE NODES
                  </div>
                  {nodes.map((n) => (
                    <NodeCard
                      key={n.id}
                      node={n}
                      selected={selectedNode}
                      onClick={setSelectedNode}
                    />
                  ))}
                </div>

                {selectedNode && (
                  <div
                    style={{
                      background: T.bg1,
                      border: `1px solid ${T.cyanDim}`,
                      borderRadius: 10,
                      padding: 16,
                      animation: "fadeIn 0.3s ease",
                    }}
                  >
                    <div
                      style={{
                        color: T.cyan,
                        fontFamily: "'JetBrains Mono',monospace",
                        fontSize: 12,
                        fontWeight: 700,
                        marginBottom: 10,
                      }}
                    >
                      {selectedNode.label}
                    </div>
                    {[
                      {
                        label: "CPU Usage",
                        value: `${selectedNode.cpu.toFixed(1)}%`,
                        color: selectedNode.cpu > 85 ? T.red : T.cyan,
                      },
                      {
                        label: "Memory",
                        value: `${selectedNode.mem.toFixed(1)}%`,
                        color: selectedNode.mem > 85 ? T.red : T.cyan,
                      },
                      {
                        label: "Status",
                        value: selectedNode.status,
                        color: statusColor(selectedNode.status),
                      },
                      {
                        label: "Layer",
                        value: selectedNode.layer,
                        color: layerColor(selectedNode.layer),
                      },
                    ].map((d, i) => (
                      <div
                        key={i}
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                          padding: "6px 0",
                          borderBottom: `1px solid ${T.border}`,
                        }}
                      >
                        <span style={{ color: T.textDim, fontSize: 11 }}>
                          {d.label}
                        </span>
                        <span
                          style={{
                            color: d.color,
                            fontSize: 11,
                            fontFamily: "'JetBrains Mono',monospace",
                            fontWeight: 600,
                          }}
                        >
                          {d.value}
                        </span>
                      </div>
                    ))}
                    {selectedNode.status !== "healthy" && (
                      <button
                        onClick={() =>
                          setGhostModal({
                            label: `Remediate ${selectedNode.label}`,
                            icon: <Wrench size={16} />,
                          })
                        }
                        style={{
                          width: "100%",
                          marginTop: 12,
                          padding: "10px",
                          background: `${T.purple}22`,
                          border: `1px solid ${T.purple}`,
                          borderRadius: 6,
                          color: T.purple,
                          fontFamily: "'JetBrains Mono',monospace",
                          fontSize: 11,
                          cursor: "pointer",
                          fontWeight: 600,
                        }}
                      >
                        👻 Ghost Preview Remediation
                      </button>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}

          {activeTab === "intelligence" && <IntelligencePanel />}

          {activeTab === "guardian" && (
            <GuardianPanel onGhostPreview={(action) => setGhostModal(action)} />
          )}

          {activeTab === "copilot" && (
            <div
              style={{
                background: T.bg1,
                border: `1px solid ${T.border}`,
                borderRadius: 10,
                padding: 20,
                height: "calc(100vh - 160px)",
                display: "flex",
                flexDirection: "column",
              }}
            >
              <div
                style={{
                  marginBottom: 16,
                  paddingBottom: 14,
                  borderBottom: `1px solid ${T.border}`,
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                }}
              >
                <div>
                  <div
                    style={{
                      color: T.textBright,
                      fontFamily: "'JetBrains Mono',monospace",
                      fontSize: 14,
                      fontWeight: 700,
                    }}
                  >
                    CCDT CO-PILOT
                  </div>
                  <div style={{ color: T.textDim, fontSize: 11, marginTop: 2 }}>
                    AI operator powered by Claude · Causal reasoning · Ghost
                    Preview integration
                  </div>
                </div>
                <div style={{ display: "flex", gap: 8 }}>
                  <GlowBadge label="CAUSAL GNN CONNECTED" color={T.cyan} />
                  <GlowBadge label="eBPF LIVE" color={T.green} />
                </div>
              </div>
              <CoPilotChat nodes={nodes} alerts={ALERTS} />
            </div>
          )}

          {activeTab === "validation" && <ValidationPanel />}
        </div>
      </div>
    </div>
  );
}

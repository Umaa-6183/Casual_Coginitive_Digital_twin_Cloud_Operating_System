import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import {
  Zap,
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
  Clock,
} from "lucide-react";

// ─── THEME ────────────────────────────────────────────────────────────────────
const T = {
  bg: "#0b1220",
  bg1: "#111827",
  bg2: "#1f2937",
  bg3: "#374151",
  border: "#112240",
  borderHi: "#1A3A6A",
  cyan: "#00D4FF",
  cyanDim: "#00D4FF55",
  cyanBright: "#60EFFF",
  green: "#00FF9F",
  greenDim: "#00FF9F33",
  red: "#FF3B5C",
  redDim: "#FF3B5C22",
  orange: "#FF8C00",
  orangeDim: "#FF8C0022",
  yellow: "#FFD60A",
  yellowDim: "#FFD60A22",
  purple: "#9B5DE5",
  purpleDim: "#9B5DE522",
  text: "#f9fafcff",
  textDim: "#ffffffff",
  textBright: "#fcfdffff",
};

const MONO = "'JetBrains Mono', 'Fira Code', monospace";
const SANS = "system-ui, -apple-system, sans-serif";

// ─── ANIMATIONS ───────────────────────────────────────────────────────────────
const CSS = `
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
@keyframes fadeIn { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }
@keyframes slideIn { from{opacity:0;transform:translateX(-10px)} to{opacity:1;transform:translateX(0)} }
@keyframes spin { to{transform:rotate(360deg)} }
@keyframes blink { 0%,49%{opacity:1} 50%,100%{opacity:0} }
@keyframes scanDown { 0%{top:-2px} 100%{top:100%} }
@keyframes glow { 0%,100%{box-shadow:0 0 6px #00D4FF33} 50%{box-shadow:0 0 18px #00D4FF88} }
@keyframes ticker { 0%{transform:translateX(0)} 100%{transform:translateX(-50%)} }
@keyframes countUp { from{opacity:0;transform:scale(.8)} to{opacity:1;transform:scale(1)} }
* { box-sizing: border-box; margin: 0; padding: 0; }
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #1A3A6A; border-radius: 2px; }
`;

// ─── SHARED ATOMS ─────────────────────────────────────────────────────────────
const Badge = ({ label, color = T.cyan, size = 10 }) => (
  <span style={{
    fontSize: size, fontFamily: MONO, letterSpacing: .8, padding: "2px 7px",
    borderRadius: 3, border: `1px solid ${color}44`, background: `${color}11`, color,
    textTransform: "uppercase", whiteSpace: "nowrap", flexShrink: 0,
  }}>{label}</span>
);

const Bar = ({ value, max = 100, color, height = 3 }) => {
  const pct = Math.min(100, (value / max) * 100);
  const col = pct > 85 ? T.red : pct > 65 ? T.yellow : (color || T.cyan);
  return (
    <div style={{ height, background: "#ffffff0A", borderRadius: 2, overflow: "hidden" }}>
      <div style={{ width: `${pct}%`, height: "100%", background: col, borderRadius: 2, boxShadow: `0 0 5px ${col}66`, transition: "width .8s ease" }} />
    </div>
  );
};

const Pill = ({ value, label, color = T.cyan, sub }) => (
  <div style={{ padding: "14px 16px", background: T.bg2, borderRadius: 8, border: `1px solid ${color}22`, textAlign: "center" }}>
    <div style={{ color, fontFamily: MONO, fontSize: 26, fontWeight: 700, lineHeight: 1, animation: "countUp .4s ease" }}>{value}</div>
    <div style={{ color: T.textDim, fontSize: 10, marginTop: 5, letterSpacing: .5 }}>{label}</div>
    {sub && <div style={{ color, fontSize: 9, marginTop: 3, fontFamily: MONO }}>{sub}</div>}
  </div>
);

const Panel = ({ title, badge, children, style = {}, headerRight }) => (
  <div style={{ background: T.bg1, border: `1px solid ${T.border}`, borderRadius: 10, overflow: "hidden", ...style }}>
    <div style={{
      padding: "11px 16px", borderBottom: `1px solid ${T.border}`,
      display: "flex", justifyContent: "space-between", alignItems: "center",
      background: `${T.bg2}88`,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ color: T.textBright, fontFamily: MONO, fontSize: 12, fontWeight: 700, letterSpacing: .5 }}>{title}</span>
        {badge && <Badge label={badge} />}
      </div>
      {headerRight}
    </div>
    <div style={{ padding: 16 }}>{children}</div>
  </div>
);

// ─── KERNEL EVENT STREAM ───────────────────────────────────────────────────────
const EVENT_TYPES = {
  syscall: { color: T.orange, icon: <Zap size={14} /> },
  oom: { color: T.red, icon: <Skull size={14} /> },
  tcp: { color: T.cyan, icon: <Globe size={14} /> },
  sched: { color: T.green, icon: <Timer size={14} /> },
  file: { color: T.purple, icon: <Folder size={14} /> },
  capability: { color: T.red, icon: <Key size={14} /> },
  probe: { color: T.textDim, icon: <Search size={14} /> },
};

const SEED_EVENTS = [
  { id: 1, ts: "14:32:07.334", type: "capability", pod: "order-svc-7f8b", node: "node-2", detail: "cap_sys_admin SET — uid=0 (root)", sev: "critical" },
  { id: 2, ts: "14:32:07.412", type: "syscall", pod: "order-svc-7f8b", node: "node-2", detail: "execve('/bin/xmrig') — cryptominer binary", sev: "critical" },
  { id: 3, ts: "14:32:07.891", type: "tcp", pod: "order-svc-7f8b", node: "node-2", detail: "outbound:4444 → 10.0.0.47 (C&C suspected)", sev: "critical" },
  { id: 4, ts: "14:32:08.021", type: "file", pod: "order-svc-7f8b", node: "node-2", detail: "open('/etc/shadow') — unauthorized read", sev: "warning" },
  { id: 5, ts: "14:32:08.340", type: "sched", pod: "order-svc-7f8b", node: "node-2", detail: "sched_latency p99=184ms (normal: 12ms)", sev: "warning" },
  { id: 6, ts: "14:32:09.001", type: "tcp", pod: "postgres-0", node: "node-1", detail: "retransmit_rate=34/s (threshold: 5/s)", sev: "warning" },
  { id: 7, ts: "14:32:09.441", type: "oom", pod: "postgres-0", node: "node-1", detail: "oom_score=742, rss=6.2GB, limit=8GB", sev: "warning" },
  { id: 8, ts: "14:32:10.112", type: "syscall", pod: "auth-svc-5c9d", node: "node-3", detail: "getpeername() × 847/s — brute force pattern", sev: "warning" },
  { id: 9, ts: "14:32:10.891", type: "probe", pod: "api-gw-3a1f", node: "node-1", detail: "http_request_duration_p99=312ms", sev: "info" },
  { id: 10, ts: "14:32:11.003", type: "sched", pod: "user-svc-2e4b", node: "node-2", detail: "cpu_throttle=22% — cgroup limit hit", sev: "info" },
];

const LIVE_EVENT_POOL = [
  { type: "tcp", pod: "ingress-ctrl", node: "node-1", detail: "connection_count=1847, new/s=34", sev: "info" },
  { type: "sched", pod: "ml-svc-4d2a", node: "node-3", detail: "softirq_time=2.1ms", sev: "info" },
  { type: "probe", pod: "kafka-0", node: "node-2", detail: "consumer_lag=12 (topic: ebpf.events)", sev: "info" },
  { type: "syscall", pod: "redis-0", node: "node-1", detail: "epoll_wait() nominal", sev: "info" },
  { type: "file", pod: "order-svc-7f8b", node: "node-2", detail: "write('/tmp/.hidden') — suspicious", sev: "warning" },
  { type: "tcp", pod: "auth-svc-5c9d", node: "node-3", detail: "SYN flood risk: 2300 half-open", sev: "warning" },
  { type: "capability", pod: "postgres-0", node: "node-1", detail: "cap_setuid attempt — denied by policy", sev: "warning" },
];

function EBPFStream() {
  const [events, setEvents] = useState(SEED_EVENTS);
  const [filter, setFilter] = useState("all");
  const [severityFilter, setSeverityFilter] = useState("all");
  const [paused, setPaused] = useState(false);
  const [stats, setStats] = useState({ total: 18472, eps: 847, critical: 3, warning: 5 });
  const bottomRef = useRef(null);
  const idRef = useRef(11);

  useEffect(() => {
    if (paused) return;
    const interval = setInterval(() => {
      const template = LIVE_EVENT_POOL[Math.floor(Math.random() * LIVE_EVENT_POOL.length)];
      const now = new Date();
      const ts = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}:${String(now.getSeconds()).padStart(2, "0")}.${String(now.getMilliseconds()).padStart(3, "0")}`;
      const newEvent = { ...template, id: idRef.current++, ts };
      setEvents(prev => [newEvent, ...prev.slice(0, 199)]);
      setStats(prev => ({
        total: prev.total + 1,
        eps: 800 + Math.floor(Math.random() * 200),
        critical: prev.critical,
        warning: prev.warning + (newEvent.sev === "warning" ? 1 : 0),
      }));
    }, 1400);
    return () => clearInterval(interval);
  }, [paused]);

  const filtered = useMemo(() => events.filter(e =>
    (filter === "all" || e.type === filter) &&
    (severityFilter === "all" || e.sev === severityFilter)
  ), [events, filter, severityFilter]);

  const sevColor = { critical: T.red, warning: T.yellow, info: T.textDim };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* Stats row */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 10 }}>
        <Pill value={stats.total.toLocaleString()} label="Total Events" color={T.cyan} sub="Since boot" />
        <Pill value={`${stats.eps}/s`} label="Events/Second" color={T.green} sub="Last 30s avg" />
        <Pill value={stats.critical} label="Critical Events" color={T.red} sub="Unresolved" />
        <Pill value={stats.warning} label="Warnings" color={T.yellow} sub="Active" />
      </div>

      {/* Probe health */}
      <Panel title="eBPF PROBE STATUS" badge="6/6 ACTIVE">
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 8 }}>
          {[
            { name: "scheduler", events: 4821, overhead: "0.12%" },
            { name: "oom_kill", events: 3, overhead: "0.01%" },
            { name: "tcp_retrans", events: 1204, overhead: "0.08%" },
            { name: "syscall", events: 9847, overhead: "0.34%" },
            { name: "file_access", events: 2103, overhead: "0.15%" },
            { name: "capability", events: 12, overhead: "0.01%" },
          ].map((p, i) => (
            <div key={i} style={{ padding: "10px 12px", background: T.bg2, borderRadius: 6, border: `1px solid ${T.border}` }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                <span style={{ color: T.green, fontFamily: MONO, fontSize: 11 }}>{p.name}</span>
                <span style={{ width: 7, height: 7, borderRadius: "50%", background: T.green, display: "inline-block", boxShadow: `0 0 5px ${T.green}`, marginTop: 2 }} />
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: T.textDim, fontSize: 10 }}>{p.events.toLocaleString()} events</span>
                <span style={{ color: T.cyan, fontSize: 10, fontFamily: MONO }}>{p.overhead} CPU</span>
              </div>
            </div>
          ))}
        </div>
      </Panel>

      {/* Event stream */}
      <Panel
        title="KERNEL EVENT STREAM"
        badge={`${filtered.length} events`}
        headerRight={
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select value={filter} onChange={e => setFilter(e.target.value)} style={{
              background: T.bg2, border: `1px solid ${T.border}`, color: T.textDim,
              fontSize: 10, fontFamily: MONO, padding: "4px 8px", borderRadius: 4, outline: "none",
            }}>
              <option value="all">All Types</option>
              {Object.keys(EVENT_TYPES).map(t => <option key={t} value={t}>{t}</option>)}
            </select>
            <select value={severityFilter} onChange={e => setSeverityFilter(e.target.value)} style={{
              background: T.bg2, border: `1px solid ${T.border}`, color: T.textDim,
              fontSize: 10, fontFamily: MONO, padding: "4px 8px", borderRadius: 4, outline: "none",
            }}>
              <option value="all">All Severity</option>
              <option value="critical">Critical</option>
              <option value="warning">Warning</option>
              <option value="info">Info</option>
            </select>
            <button onClick={() => setPaused(p => !p)} style={{
              padding: "4px 10px", background: paused ? `${T.green}22` : `${T.red}22`,
              border: `1px solid ${paused ? T.green : T.red}55`, borderRadius: 4,
              color: paused ? T.green : T.red, fontSize: 10, fontFamily: MONO, cursor: "pointer",
            }}>{paused ? "▶ RESUME" : "⏸ PAUSE"}</button>
          </div>
        }
      >
        <div style={{ maxHeight: 420, overflowY: "auto", fontFamily: MONO, fontSize: 11 }}>
          {/* Header */}
          <div style={{
            display: "grid", gridTemplateColumns: "120px 80px 140px 120px 1fr 70px",
            gap: 8, padding: "4px 8px", color: T.textDim, fontSize: 9,
            borderBottom: `1px solid ${T.border}`, marginBottom: 4, letterSpacing: .5,
          }}>
            <span>TIMESTAMP</span><span>TYPE</span><span>POD</span><span>NODE</span><span>DETAIL</span><span>SEV</span>
          </div>
          {filtered.map((e, i) => {
            const meta = EVENT_TYPES[e.type] || EVENT_TYPES.probe;
            return (
              <div key={e.id} style={{
                display: "grid", gridTemplateColumns: "120px 80px 140px 120px 1fr 70px",
                gap: 8, padding: "5px 8px",
                background: e.sev === "critical" ? `${T.red}08` : i % 2 === 0 ? T.bg2 : "transparent",
                borderRadius: 3, marginBottom: 1,
                borderLeft: e.sev === "critical" ? `2px solid ${T.red}` : e.sev === "warning" ? `2px solid ${T.yellow}` : "2px solid transparent",
                animation: i < 3 ? "slideIn .3s ease" : "none",
              }}>
                <span style={{ color: T.textDim }}>{e.ts}</span>
                <span style={{ color: meta.color }}>{meta.icon} {e.type}</span>
                <span style={{ color: T.textBright, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{e.pod}</span>
                <span style={{ color: T.textDim }}>{e.node}</span>
                <span style={{ color: e.sev === "critical" ? T.red : e.sev === "warning" ? T.yellow : T.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{e.detail}</span>
                <span style={{ color: sevColor[e.sev] || T.textDim }}><Badge label={e.sev} color={sevColor[e.sev] || T.textDim} size={9} /></span>
              </div>
            );
          })}
          <div ref={bottomRef} />
        </div>
      </Panel>
    </div>
  );
}

// ─── INCIDENT MANAGER ─────────────────────────────────────────────────────────
const INCIDENTS = [
  {
    id: "INC-2847",
    title: "Cryptominer detected in order-svc via privilege escalation",
    severity: "critical",
    status: "active",
    type: "attack",
    opened: "14:32:07",
    mttr_target: "8 min",
    elapsed: "6m 23s",
    node: "order-svc-7f8b / node-2",
    root_cause:
      "Container escape via CAP_SYS_ADMIN → xmrig binary execution → C&C beaconing",
    affected: ["order-svc", "postgres", "user-svc"],
    confidence: 94,
    auto_action: "Pending Ghost Preview approval",
    timeline: [
      {
        t: "14:32:07",
        event: "eBPF: privilege escalation detected",
        icon: <Zap size={14} />, color: T.red,
      },
      {
        t: "14:32:08",
        event: "GNN: root cause identified (94% confidence)",
        icon: <Brain size={14} />, color: T.purple,
      },
      {
        t: "14:32:08",
        event: "GNN: classified as ATTACK (not fault)",
        icon: <AlertCircle size={14} />, color: T.red,
      },
      {
        t: "14:32:09",
        event: "RL: action proposed — isolate + block IP",
        icon: <Shield size={14} />, color: T.cyan,
      },
      {
        t: "14:32:09",
        event: "Ghost Preview: simulation complete (PASS)",
        icon: <Ghost size={14} />, color: T.green,
      },
      {
        t: "14:32:09",
        event: "OPA: all 5 policies satisfied",
        icon: <CheckCircle size={14} />, color: T.green,
      },
      {
        t: "14:32:10",
        event: "Awaiting SRE approval (human-in-the-loop mode)",
        icon: <Hourglass size={14} />, color: T.yellow,
      },
    ],
  },
  {
    id: "INC-2846",
    title: "PostgreSQL memory pressure — OOM risk at 79%",
    severity: "warning",
    status: "investigating",
    type: "fault",
    opened: "14:31:55",
    mttr_target: "15 min",
    elapsed: "6m 35s",
    node: "postgres-0 / node-1",
    root_cause:
      "High connection count from order-svc (cascading from INC-2847) + missing connection pool limit",
    affected: ["postgres", "user-svc"],
    confidence: 81,
    auto_action: "RL: increase memory limit + restart with new config",
    timeline: [
      {
        t: "14:31:55",
        event: "eBPF: OOM score 742 detected",
        icon: <Zap size={14} />, color: T.red,
      },
      {
        t: "14:31:56",
        event: "GNN: causal link to INC-2847 established",
        icon: <Brain size={14} />, color: T.purple,
      },
      {
        t: "14:31:57",
        event: "GNN: classified as FAULT (cascading)",
        icon: <AlertTriangle size={14} />, color: T.yellow,
      },
      {
        t: "14:31:58",
        event: "RL: action proposed — memory limit + restart",
        icon: <Shield size={14} />, color: T.cyan,
      },
    ],
  },
  {
    id: "INC-2845",
    title: "Auth service brute-force attempt pattern detected",
    severity: "warning",
    status: "auto-resolved",
    type: "attack",
    opened: "14:31:22",
    mttr_target: "5 min",
    elapsed: "resolved in 2m 11s",
    node: "auth-svc-5c9d / node-3",
    root_cause:
      "847 getpeername() calls/sec from 3 external IPs — rate limit triggered",
    affected: ["auth-svc"],
    confidence: 88,
    auto_action: "IP rate-limit applied autonomously (OPA approved)",
    timeline: [
      {
        t: "14:31:22",
        event: "eBPF: syscall anomaly detected (847/s)",
        icon: <Zap size={14} />, color: T.red,
      },
      {
        t: "14:31:23",
        event: "GNN: brute force pattern (88% confidence)",
        icon: <Brain size={14} />, color: T.purple,
      },
      {
        t: "14:31:24",
        event: "RL + OPA: rate-limit approved",
        icon: <CheckCircle size={14} />, color: T.green,
      },
      {
        t: "14:31:33",
        event: "Executor: NetworkPolicy applied",
        icon: <Settings size={14} />, color: T.cyan,
      },
      {
        t: "14:33:33",
        event: "RESOLVED — attack traffic dropped to 0",
        icon: <CheckCircle size={14} />, color: T.green,
      },
    ],
  },
  {
    id: "INC-2844",
    title: "Scheduler latency spike on node-3 (p99: 184ms)",
    severity: "info",
    status: "resolved",
    type: "fault",
    opened: "14:29:18",
    mttr_target: "10 min",
    elapsed: "resolved in 3m 44s",
    node: "node-3 (cluster-level)",
    root_cause:
      "CPU resource contention from ML service batch job — autoscaler triggered",
    affected: ["ml-svc", "ingress"],
    confidence: 76,
    auto_action: "ML-svc replica scaling applied (×2)",
    timeline: [
      {
        t: "14:29:18",
        event: "eBPF: sched_latency p99 spike",
        icon: <Zap size={14} />, color: T.red,
      },
      {
        t: "14:29:19",
        event: "GNN: contention root cause identified",
        icon: <Brain size={14} />, color: T.purple,
      },
      {
        t: "14:29:20",
        event: "RL: scale ml-svc replicas proposed",
        icon: <Shield size={14} />, color: T.cyan,
      },
      {
        t: "14:29:22",
        event: "HPA scaling triggered autonomously",
        icon: <Settings size={14} />, color: T.cyan,
      },
      {
        t: "14:33:02",
        event: "RESOLVED — latency back to 11ms p99",
        icon: <CheckCircle size={14} />, color: T.green,
      },
    ],
  },
];

function IncidentManager() {
  const [selected, setSelected] = useState(INCIDENTS[0]);
  const [filterStatus, setFilterStatus] = useState("all");

  const statusColor = { active: T.red, investigating: T.yellow, "auto-resolved": T.green, resolved: T.textDim };
  const sevColor = { critical: T.red, warning: T.yellow, info: T.cyan };

  const filtered = INCIDENTS.filter(i => filterStatus === "all" || i.status === filterStatus);

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "360px 1fr",
        gap: 16,
        height: "100%",
      }}
    >
      {/* Incident List */}
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr 1fr 1fr",
            gap: 6,
          }}
        >
          {[
            { label: "active", count: 1, color: T.red },
            { label: "investigating", count: 1, color: T.yellow },
            { label: "auto-resolved", count: 1, color: T.green },
            { label: "resolved", count: 1, color: T.textDim },
          ].map((s) => (
            <button
              key={s.label}
              onClick={() =>
                setFilterStatus((prev) => (prev === s.label ? "all" : s.label))
              }
              style={{
                padding: "8px 4px",
                background: filterStatus === s.label ? `${s.color}22` : T.bg2,
                border: `1px solid ${filterStatus === s.label ? s.color : T.border}`,
                borderRadius: 6,
                color: s.color,
                fontFamily: MONO,
                fontSize: 9,
                cursor: "pointer",
                textAlign: "center",
                transition: "all .2s",
                textTransform: "uppercase",
              }}
            >
              <div style={{ fontSize: 18, fontWeight: 700, marginBottom: 2 }}>
                {s.count}
              </div>
              <div>{s.label}</div>
            </button>
          ))}
        </div>

        <div
          style={{
            flex: 1,
            overflowY: "auto",
            display: "flex",
            flexDirection: "column",
            gap: 8,
          }}
        >
          {filtered.map((inc) => (
            <div
              key={inc.id}
              onClick={() => setSelected(inc)}
              style={{
                padding: "14px",
                background: selected.id === inc.id ? `${T.cyan}0D` : T.bg1,
                border: `1px solid ${selected.id === inc.id ? T.cyanDim : T.border}`,
                borderLeft: `3px solid ${sevColor[inc.severity]}`,
                borderRadius: 8,
                cursor: "pointer",
                transition: "all .2s",
                boxShadow:
                  selected.id === inc.id ? `0 0 12px ${T.cyanDim}` : "none",
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  marginBottom: 6,
                }}
              >
                <span
                  style={{ color: T.textDim, fontFamily: MONO, fontSize: 10 }}
                >
                  {inc.id}
                </span>
                <Badge
                  label={inc.status}
                  color={statusColor[inc.status]}
                  size={9}
                />
              </div>
              <div
                style={{
                  color: T.textBright,
                  fontSize: 12,
                  lineHeight: 1.4,
                  marginBottom: 8,
                }}
              >
                {inc.title}
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                <Badge
                  label={inc.severity}
                  color={sevColor[inc.severity]}
                  size={9}
                />
                <Badge
                  label={inc.type}
                  color={inc.type === "attack" ? T.red : T.orange}
                  size={9}
                />
                <span
                  style={{
                    color: T.textDim,
                    fontSize: 9,
                    fontFamily: MONO,
                    marginLeft: "auto",
                  }}
                >
                  <Clock size={12} color={T.green} />
                  {inc.elapsed}
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Incident Detail */}
      {selected && (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 12,
            overflowY: "auto",
            animation: "fadeIn .3s ease",
          }}
        >
          {/* Header */}
          <div
            style={{
              padding: "16px 20px",
              background: T.bg1,
              border: `1px solid ${T.border}`,
              borderLeft: `3px solid ${sevColor[selected.severity]}`,
              borderRadius: 10,
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
              <div>
                <span
                  style={{ color: T.textDim, fontFamily: MONO, fontSize: 10 }}
                >
                  {selected.id}
                </span>
                <div
                  style={{
                    color: T.textBright,
                    fontSize: 15,
                    fontWeight: 600,
                    marginTop: 4,
                    lineHeight: 1.3,
                  }}
                >
                  {selected.title}
                </div>
              </div>
              <Badge
                label={selected.status}
                color={statusColor[selected.status]}
              />
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <Badge
                label={selected.severity}
                color={sevColor[selected.severity]}
              />
              <Badge
                label={selected.type}
                color={selected.type === "attack" ? T.red : T.orange}
              />
              <Badge label={`GNN ${selected.confidence}%`} color={T.cyan} />
              <Badge label={`opened ${selected.opened}`} color={T.textDim} />
            </div>
          </div>

          {/* Metrics row */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(3,1fr)",
              gap: 10,
            }}
          >
            <Pill
              value={selected.elapsed}
              label="Elapsed"
              color={selected.status === "active" ? T.red : T.green}
            />
            <Pill
              value={selected.affected.length}
              label="Services Affected"
              color={T.orange}
              sub={selected.affected.join(", ")}
            />
            <Pill
              value={`${selected.confidence}%`}
              label="GNN Confidence"
              color={T.cyan}
            />
          </div>

          {/* Root cause */}
          <Panel title="ROOT CAUSE (Causal GNN)" badge="CAUSAL ANALYSIS">
            <div
              style={{
                padding: "12px 14px",
                background: T.bg2,
                borderRadius: 6,
                border: `1px solid ${T.border}`,
              }}
            >
              <div
                style={{
                  color: T.textDim,
                  fontSize: 10,
                  fontFamily: MONO,
                  marginBottom: 4,
                }}
              >
                CAUSAL CHAIN
              </div>
              <div
                style={{ color: T.textBright, fontSize: 13, lineHeight: 1.5 }}
              >
                {selected.root_cause}
              </div>
            </div>
            <div
              style={{
                marginTop: 10,
                padding: "12px 14px",
                background: `${T.purple}0D`,
                borderRadius: 6,
                border: `1px solid ${T.purpleDim}`,
              }}
            >
              <div
                style={{
                  color: T.textDim,
                  fontSize: 10,
                  fontFamily: MONO,
                  marginBottom: 4,
                }}
              >
                AUTONOMOUS ACTION
              </div>
              <div style={{ color: T.purple, fontSize: 12 }}>
                {selected.auto_action}
              </div>
            </div>
          </Panel>

          {/* Timeline */}
          <Panel title="INCIDENT TIMELINE">
            <div style={{ position: "relative", paddingLeft: 24 }}>
              <div
                style={{
                  position: "absolute",
                  left: 8,
                  top: 0,
                  bottom: 0,
                  width: 2,
                  background: T.border,
                  borderRadius: 1,
                }}
              />
              {selected.timeline.map((t, i) => (
                <div
                  key={i}
                  style={{
                    position: "relative",
                    marginBottom: 12,
                    animation: `fadeIn .3s ease ${i * 0.08}s both`,
                  }}
                >
                  <div
                    style={{
                      position: "absolute",
                      left: -20,
                      top: 2,
                      width: 16,
                      height: 16,
                      borderRadius: "50%",
                      background: T.bg1,
                      border: `1.5px solid ${T.cyanDim}`,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      fontSize: 8,
                      zIndex: 1,
                    }}
                  >
                    {t.icon}
                  </div>
                  <div
                    style={{ display: "flex", gap: 10, alignItems: "baseline" }}
                  >
                    <span
                      style={{
                        color: T.textDim,
                        fontFamily: MONO,
                        fontSize: 10,
                        flexShrink: 0,
                      }}
                    >
                      {t.t}
                    </span>
                    <span
                      style={{ color: T.text, fontSize: 12, lineHeight: 1.4 }}
                    >
                      {t.event}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </Panel>

          {/* Affected services */}
          <Panel title="AFFECTED SERVICES">
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {selected.affected.map((svc) => (
                <div
                  key={svc}
                  style={{
                    padding: "10px 16px",
                    background: T.bg2,
                    border: `1px solid ${T.redDim}`,
                    borderRadius: 6,
                  }}
                >
                  <div
                    style={{
                      color: T.textBright,
                      fontFamily: MONO,
                      fontSize: 12,
                    }}
                  >
                    {svc}
                  </div>
                  <div style={{ color: T.red, fontSize: 10, marginTop: 2 }}>
                    IMPACTED
                  </div>
                </div>
              ))}
              {!selected.affected.find((s) => s) && (
                <div style={{ color: T.textDim, fontSize: 12 }}>
                  No affected services
                </div>
              )}
            </div>
          </Panel>
        </div>
      )}
    </div>
  );
}

// ─── NOTIFICATION CENTER ───────────────────────────────────────────────────────
function NotificationCenter() {
  const [notifications, setNotifications] = useState([
    { id: 1, time: "14:32:10", type: "alert", title: "Critical: Cryptominer in order-svc", msg: "Privilege escalation + xmrig binary detected. GNN confidence 94%. Ghost Preview ready.", read: false, color: T.red },
    { id: 2, time: "14:32:09", type: "action", title: "Ghost Preview completed: Isolate order-svc", msg: "Simulation passed. MTTR impact: -68%. OPA: PASS. Risk: LOW. Awaiting approval.", read: false, color: T.purple },
    { id: 3, time: "14:31:55", type: "alert", title: "Warning: PostgreSQL OOM risk (79%)", msg: "Memory pressure cascade from INC-2847. RL proposes limit increase + restart.", read: false, color: T.yellow },
    { id: 4, time: "14:31:33", type: "resolved", "title": "Auto-resolved: Auth brute force (INC-2845)", msg: "NetworkPolicy applied autonomously. Attack traffic dropped to 0. MTTR: 2m 11s.", read: true, color: T.green },
    { id: 5, time: "14:30:12", type: "policy", title: "OPA Policy Violation: Egress to unknown IP", msg: "order-svc attempted TCP to 10.0.0.47:4444. Blocked by egress_control.rego.", read: true, color: T.red },
    { id: 6, time: "14:29:44", type: "info", title: "GNN model retrained: +2.1% accuracy", msg: "Causal GNN retrained on 12 new labeled incidents. F1 improved from 0.914 → 0.935.", read: true, color: T.cyan },
    { id: 7, time: "14:28:55", type: "resolved", "title": "Auto-resolved: Scheduler latency (INC-2844)", msg: "ML-svc scaled ×2. Latency normalized to 11ms p99. MTTR: 3m 44s.", read: true, color: T.green },
    { id: 8, time: "14:25:00", type: "info", title: "Phase 4 deployment: Guardian Layer online", msg: "RL agent, OPA engine, and Ghost Preview simulator all operational. 5 policies active.", read: true, color: T.cyan },
  ]);

  const [filter, setFilter] = useState("all");
  const unread = notifications.filter(n => !n.read).length;

  const typeIcon = {
    alert: <AlertCircle size={14} />,
    action: <Ghost size={14} />,
    resolved: <CheckCircle size={14} />,
    policy: <Shield size={14} />,
    info: <Info size={14} />,
  };

  const filtered = notifications.filter(n => filter === "all" || n.type === filter);

  const markAll = () => setNotifications(prev => prev.map(n => ({ ...n, read: true })));
  const markRead = (id) => setNotifications(prev => prev.map(n => n.id === id ? { ...n, read: true } : n));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* Header stats */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(5,1fr)", gap: 10 }}>
        {["all", "alert", "action", "policy", "info"].map(f => {
          const count = f === "all" ? notifications.length : notifications.filter(n => n.type === f).length;
          const col = { all: T.cyan, alert: T.red, action: T.purple, policy: T.orange, info: T.textDim }[f];
          return (
            <button key={f} onClick={() => setFilter(f)} style={{
              padding: "10px 8px", background: filter === f ? `${col}18` : T.bg2,
              border: `1px solid ${filter === f ? col : T.border}`, borderRadius: 6,
              cursor: "pointer", textAlign: "center", transition: "all .2s",
            }}>
              <div style={{ color: col, fontFamily: MONO, fontSize: 18, fontWeight: 700 }}>{count}</div>
              <div style={{ color: T.textDim, fontSize: 9, textTransform: "uppercase", letterSpacing: .5, marginTop: 2 }}>{f}</div>
            </button>
          );
        })}
      </div>

      <Panel title="NOTIFICATION CENTER" badge={unread > 0 ? `${unread} UNREAD` : "ALL READ"}
        headerRight={
          <button onClick={markAll} style={{
            padding: "4px 10px", background: T.bg2, border: `1px solid ${T.border}`,
            borderRadius: 4, color: T.textDim, fontSize: 10, fontFamily: MONO, cursor: "pointer",
          }}>Mark all read</button>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 8, maxHeight: 520, overflowY: "auto" }}>
          {filtered.map((n, i) => (
            <div key={n.id} onClick={() => markRead(n.id)} style={{
              padding: "14px 16px", background: n.read ? T.bg2 : `${n.color}08`,
              border: `1px solid ${n.read ? T.border : n.color + "44"}`,
              borderLeft: `3px solid ${n.read ? T.border : n.color}`,
              borderRadius: 8, cursor: "pointer", transition: "all .2s",
              animation: `fadeIn .3s ease ${i * .05}s both`,
            }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 6 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ fontSize: 14 }}>{typeIcon[n.type]}</span>
                  <span style={{ color: n.read ? T.text : T.textBright, fontSize: 13, fontWeight: n.read ? 400 : 600 }}>{n.title}</span>
                  {!n.read && <span style={{ width: 7, height: 7, borderRadius: "50%", background: n.color, display: "inline-block", boxShadow: `0 0 5px ${n.color}` }} />}
                </div>
                <span style={{ color: T.textDim, fontFamily: MONO, fontSize: 10, flexShrink: 0, marginLeft: 12 }}>{n.time}</span>
              </div>
              <div style={{ color: T.textDim, fontSize: 12, lineHeight: 1.5 }}>{n.msg}</div>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

// ─── SETTINGS ─────────────────────────────────────────────────────────────────
function Settings() {
  const [settings, setSettings] = useState({
    autonomy_mode: "human-in-loop",
    ebpf_sample_rate: 100,
    gnn_confidence_threshold: 80,
    alert_cooldown: 30,
    ghost_preview: true,
    opa_enforce: true,
    llm_streaming: true,
    notifications_critical: true,
    notifications_warning: true,
    notifications_info: false,
    mttr_target: 10,
    log_retention: 7,
    replica_limit: 10,
  });

  const toggle = (key) => setSettings(prev => ({ ...prev, [key]: !prev[key] }));
  const set = (key, val) => setSettings(prev => ({ ...prev, [key]: val }));

  const Toggle = ({ k, label, desc, color = T.cyan }) => (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "12px 0", borderBottom: `1px solid ${T.border}` }}>
      <div>
        <div style={{ color: T.textBright, fontSize: 13 }}>{label}</div>
        <div style={{ color: T.textDim, fontSize: 11, marginTop: 2 }}>{desc}</div>
      </div>
      <div onClick={() => toggle(k)} style={{
        width: 42, height: 22, borderRadius: 11,
        background: settings[k] ? `${color}33` : T.bg3,
        border: `1px solid ${settings[k] ? color : T.border}`,
        position: "relative", cursor: "pointer", flexShrink: 0, marginLeft: 16,
        transition: "all .2s",
      }}>
        <div style={{
          width: 16, height: 16, borderRadius: "50%",
          background: settings[k] ? color : T.textDim,
          position: "absolute", top: 2, left: settings[k] ? 22 : 2,
          transition: "left .2s", boxShadow: settings[k] ? `0 0 6px ${color}` : "none",
        }} />
      </div>
    </div>
  );

  const Slider = ({ k, label, min, max, unit, color = T.cyan }) => (
    <div style={{ padding: "12px 0", borderBottom: `1px solid ${T.border}` }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
        <span style={{ color: T.textBright, fontSize: 13 }}>{label}</span>
        <span style={{ color, fontFamily: MONO, fontSize: 13, fontWeight: 700 }}>{settings[k]}{unit}</span>
      </div>
      <input type="range" min={min} max={max} value={settings[k]}
        onChange={e => set(k, Number(e.target.value))}
        style={{ width: "100%", accentColor: color, cursor: "pointer" }} />
      <div style={{ display: "flex", justifyContent: "space-between", marginTop: 2 }}>
        <span style={{ color: T.textDim, fontSize: 10 }}>{min}{unit}</span>
        <span style={{ color: T.textDim, fontSize: 10 }}>{max}{unit}</span>
      </div>
    </div>
  );

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
      {/* Autonomy */}
      <Panel title="AUTONOMY LEVEL">
        <div style={{ marginBottom: 16 }}>
          <div style={{ color: T.textDim, fontSize: 11, fontFamily: MONO, marginBottom: 10 }}>OPERATION MODE</div>
          {[
            { val: "human-in-loop", label: "Human-in-the-Loop", desc: "All actions require SRE approval via Ghost Preview", color: T.cyan },
            { val: "supervised", label: "Supervised Autonomy", desc: "Low-risk actions auto-execute; high-risk need approval", color: T.yellow },
            { val: "full-auto", label: "Full Autonomy", desc: "All OPA-approved actions execute automatically (Level 4)", color: T.green },
          ].map(m => (
            <div key={m.val} onClick={() => set("autonomy_mode", m.val)} style={{
              padding: "12px 14px", marginBottom: 8,
              background: settings.autonomy_mode === m.val ? `${m.color}11` : T.bg2,
              border: `1px solid ${settings.autonomy_mode === m.val ? m.color : T.border}`,
              borderRadius: 6, cursor: "pointer", transition: "all .2s",
            }}>
              <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 4 }}>
                <div style={{
                  width: 14, height: 14, borderRadius: "50%",
                  border: `2px solid ${m.color}`,
                  background: settings.autonomy_mode === m.val ? m.color : "transparent",
                  boxShadow: settings.autonomy_mode === m.val ? `0 0 6px ${m.color}` : "none",
                  transition: "all .2s",
                }} />
                <span style={{ color: settings.autonomy_mode === m.val ? m.color : T.textBright, fontSize: 13, fontWeight: 600 }}>{m.label}</span>
              </div>
              <div style={{ color: T.textDim, fontSize: 11, paddingLeft: 24 }}>{m.desc}</div>
            </div>
          ))}
        </div>
        <Toggle k="ghost_preview" label="Ghost Preview Required" desc="Simulate all actions before execution" color={T.purple} />
        <Toggle k="opa_enforce" label="OPA Hard Enforcement" desc="Block actions that violate any Rego policy" color={T.red} />
        <Slider k="gnn_confidence_threshold" label="Minimum GNN Confidence" min={50} max={99} unit="%" />
      </Panel>

      {/* eBPF & Performance */}
      <Panel title="eBPF & PERFORMANCE">
        <Slider k="ebpf_sample_rate" label="eBPF Sample Rate" min={1} max={100} unit="%" color={T.green} />
        <Slider k="alert_cooldown" label="Alert Cooldown Period" min={10} max={300} unit="s" color={T.orange} />
        <Slider k="mttr_target" label="MTTR Target" min={1} max={60} unit=" min" />
        <Slider k="log_retention" label="Event Log Retention" min={1} max={30} unit=" days" />
        <Slider k="replica_limit" label="Max Auto-Scale Replicas" min={2} max={20} unit="" color={T.purple} />
      </Panel>

      {/* Notifications */}
      <Panel title="NOTIFICATIONS">
        <Toggle k="notifications_critical" label="Critical Alerts" desc="Immediate push notification for critical severity" color={T.red} />
        <Toggle k="notifications_warning" label="Warning Alerts" desc="Notify on warning-level incidents" color={T.yellow} />
        <Toggle k="notifications_info" label="Info Events" desc="Informational events (high volume)" color={T.textDim} />
        <Toggle k="llm_streaming" label="LLM Streaming" desc="Stream Co-Pilot responses token by token" color={T.cyan} />
      </Panel>

      {/* System Info */}
      <Panel title="SYSTEM INFORMATION">
        {[
          { label: "Platform Version", value: "CCDT v1.0.0" },
          { label: "GNN Model", value: "causal-gat-v3.2" },
          { label: "LLM Backend", value: "claude-sonnet-4" },
          { label: "OPA Version", value: "v0.65.0" },
          { label: "eBPF Framework", value: "cilium/ebpf v0.15" },
          { label: "Kubernetes Version", value: "v1.30.2" },
          { label: "Kafka Brokers", value: "3 (healthy)" },
          { label: "Active Policies", value: "5 / 5" },
          { label: "Uptime", value: "14d 6h 23m" },
          { label: "Last GNN Retrain", value: "2026-03-08 14:29" },
        ].map((row, i) => (
          <div key={i} style={{ display: "flex", justifyContent: "space-between", padding: "8px 0", borderBottom: `1px solid ${T.border}` }}>
            <span style={{ color: T.textDim, fontSize: 12 }}>{row.label}</span>
            <span style={{ color: T.cyan, fontSize: 12, fontFamily: MONO }}>{row.value}</span>
          </div>
        ))}
        <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
          {["Export Config", "Reset Defaults", "Download Logs"].map(a => (
            <button key={a} style={{
              flex: 1, padding: "8px 4px", background: T.bg2, border: `1px solid ${T.border}`,
              borderRadius: 6, color: T.textDim, fontSize: 10, fontFamily: MONO, cursor: "pointer",
              transition: "all .2s",
            }}
              onMouseEnter={e => { e.target.style.borderColor = T.cyan; e.target.style.color = T.cyan; }}
              onMouseLeave={e => { e.target.style.borderColor = T.border; e.target.style.color = T.textDim; }}>
              {a}
            </button>
          ))}
        </div>
      </Panel>
    </div>
  );
}

// ─── TICKER BAR ───────────────────────────────────────────────────────────────
function TickerBar() {
  const items = [
    { icon: <Zap size={12} />, text: "eBPF: 847 events/sec", color: T.green },
    {
      icon: <AlertCircle size={12} />,
      text: "INC-2847: Cryptominer active in order-svc — Ghost Preview ready",
      color: T.red,
    },
    {
      icon: <Brain size={12} />,
      text: "GNN: 94.2% confidence on root cause identification",
      color: T.purple,
    },
    {
      icon: <Shield size={12} />,
      text: "OPA: 5/5 policies active — 2 violations flagged",
      color: T.green,
    },
    {
      icon: <CheckCircle size={12} />,
      text: "INC-2845: Auto-resolved in 2m 11s",
      color: T.cyan,
    },
    { icon: <BarChart3 size={12} />, text: "MTTR reduction: 68% vs baseline", color: T.orange },
    {
      icon: <Globe size={12} />,
      text: "Cluster: 10 nodes healthy — 3 incidents tracked",
      color: T.textDim,
    },
    {
      icon: <MessageSquare size={12} />,
      text: "Co-Pilot: claude-sonnet-4 online — causal reasoning active",
      color: T.cyan,
    },
  ];

  const ticker = [...items, ...items];

  return (
    <div
      style={{
        height: 28,
        background: T.bg2,
        borderBottom: `1px solid ${T.border}`,
        overflow: "hidden",
        position: "relative",
      }}
    >
      <div
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 24,
          whiteSpace: "nowrap",
          color: T.textDim,
          fontSize: 10,
          fontFamily: MONO,
          letterSpacing: 0.4,
          animation: "ticker 40s linear infinite",
          paddingTop: 6,
        }}
      >
        {ticker.map((item, i) => (
          <div
            key={i}
            style={{ display: "flex", alignItems: "center", gap: 6 }}
          >
            {item.icon}
            <span>{item.text}</span>
            <span style={{ opacity: 0.3 }}>·</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── MAIN APP ─────────────────────────────────────────────────────────────────


const NAV = [
  { id: "ebpf", icon: <Zap size={16} />, color: T.cyan, label: "eBPF Sensors", badge: "LIVE" },
  {
    id: "incidents",
    icon: <AlertTriangle size={16} />,
    color: T.red,
    label: "Incidents",
    badge: "2 ACTIVE",
  },
  {
    id: "notify",
    icon: <Bell size={16} />,
    color: T.cyan,
    label: "Notifications",
    badge: "3 NEW",
  },
  {
    id: "settings",
    icon: <Settings size={16} />,
    color: T.green,
    label: "Settings",
    badge: null,
  },
];

export default function CCDTPart2() {
  const [tab, setTab] = useState("ebpf");
  const [clock, setClock] = useState(new Date());

  useEffect(() => {
    const i = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(i);
  }, []);

  return (
    <div style={{ background: T.bg, minHeight: "100vh", color: T.text, fontFamily: SANS }}>
      <style>{CSS}</style>

      {/* Top bar */}
      <div style={{
        height: 56, background: T.bg1, borderBottom: `1px solid ${T.border}`,
        display: "flex", alignItems: "center", padding: "0 24px",
        justifyContent: "space-between", position: "sticky", top: 0, zIndex: 100,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 20 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{ width: 34, height: 34, borderRadius: 8, background: `linear-gradient(135deg,${T.cyan}22,${T.purple}22)`, border: `1px solid ${T.cyanDim}`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 18 }}>⬡</div>
            <div>
              <div style={{ color: T.cyanBright, fontFamily: MONO, fontSize: 13, fontWeight: 700, letterSpacing: 1 }}>CCDT</div>
              <div style={{ color: T.textDim, fontSize: 9, letterSpacing: .5 }}>EXTENDED PLATFORM — PART 2</div>
            </div>
          </div>
          {/* Nav tabs */}
          <div style={{ display: "flex", gap: 4, marginLeft: 16 }}>
            {NAV.map(n => (
              <button key={n.id} onClick={() => setTab(n.id)} style={{
                padding: "6px 14px", background: tab === n.id ? `${T.cyan}15` : "transparent",
                border: `1px solid ${tab === n.id ? T.cyanDim : "transparent"}`,
                borderRadius: 6, color: tab === n.id ? T.cyan : T.textDim,
                fontFamily: MONO, fontSize: 11, cursor: "pointer", display: "flex", alignItems: "center", gap: 7, transition: "all .15s",
              }}>
                {n.icon} {n.label}
                {n.badge && <Badge label={n.badge} color={tab === n.id ? T.cyan : T.textDim} size={8} />}
              </button>
            ))}
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <span style={{ width: 7, height: 7, borderRadius: "50%", background: T.red, display: "inline-block", animation: "pulse 1.2s infinite" }} />
            <span style={{ color: T.red, fontFamily: MONO, fontSize: 11, fontWeight: 700 }}>2 ACTIVE INCIDENTS</span>
          </div>
          <span style={{ color: T.textDim, fontFamily: MONO, fontSize: 12, borderLeft: `1px solid ${T.border}`, paddingLeft: 16 }}>
            {clock.toLocaleTimeString("en-US", { hour12: false })}
          </span>
        </div>
      </div>

      {/* Ticker */}
      <TickerBar />

      {/* Main */}
      <div style={{ padding: 20, maxWidth: 1600, margin: "0 auto" }}>
        {tab === "ebpf" && <EBPFStream />}
        {tab === "incidents" && <IncidentManager />}
        {tab === "notify" && <NotificationCenter />}
        {tab === "settings" && <Settings />}
      </div>
    </div>
  );
}

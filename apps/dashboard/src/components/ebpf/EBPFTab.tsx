import React from 'react';
import { useEBPFStream } from '@/hooks/useEBPFStream';
import { GlowBadge }    from '@/components/shared/GlowBadge';
import type { EBPFEventType } from '@/types';
import {
  Settings,
  Flame,
  Globe,
  Clock,
  Folder,
  Key,
  Radar,
} from "lucide-react";
const TYPE_COLORS: Record<EBPFEventType, string> = {
  syscall:    '#FF3B5C',
  oom:        '#FF3B5C',
  tcp:        '#FFB800',
  sched:      '#FFB800',
  file:       '#FF3B5C',
  capability: '#FF3B5C',
  probe:      '#00D4FF',
};

const TYPE_ICONS: Record<EBPFEventType, React.ReactNode> = {
  syscall: <Settings size={14} color="#A78BFA" />,
  oom: <Flame size={14} color="#F87171" />,
  tcp: <Globe size={14} color="#22D3EE" />,
  sched: <Clock size={14} color="#FBBF24" />,
  file: <Folder size={14} color="#34D399" />,
  capability: <Key size={14} color="#F87171" />,
  probe: <Radar size={14} color="#94A3B8" />,
};

const SEV_COLORS = { critical: '#FF3B5C', warning: '#FFB800', info: '#00D4FF' };

const FILTER_TYPES: { id: EBPFEventType | 'all'; label: string }[] = [
  { id: 'all',        label: 'All'         },
  { id: 'syscall',    label: 'Syscall'     },
  { id: 'oom',        label: 'OOM'         },
  { id: 'tcp',        label: 'TCP'         },
  { id: 'sched',      label: 'Sched'       },
  { id: 'file',       label: 'File'        },
  { id: 'capability', label: 'Capability'  },
  { id: 'probe',      label: 'Probe'       },
];

const PROBES = [
  { name: 'scheduler',    overhead: '0.12%', events: 84320 },
  { name: 'oom_kill',     overhead: '0.01%', events: 3      },
  { name: 'tcp_retransmit',overhead:'0.08%', events: 12440  },
  { name: 'syscall',      overhead: '0.34%', events: 421800 },
  { name: 'file_access',  overhead: '0.15%', events: 18200  },
  { name: 'capability',   overhead: '0.01%', events: 14     },
];

export const EBPFTab: React.FC = () => {
  const { events, paused, setPaused, filter, setFilter, stats } = useEBPFStream();

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>

      {/* Stats bar */}
      <div style={{
        padding: '10px 20px', borderBottom: '1px solid #0D2244',
        display: 'flex', alignItems: 'center', gap: 20, flexShrink: 0, background: '#040C1A',
        flexWrap: 'wrap',
      }}>
        {[
          { label: 'Events in Buffer', value: stats.total,    color: '#00D4FF' },
          { label: 'Critical',         value: stats.critical, color: '#FF3B5C' },
          { label: 'Warning',          value: stats.warning,  color: '#FFB800' },
          { label: 'Events/sec',       value: `~${stats.rate}`, color: '#00FF9F' },
        ].map(s => (
          <div key={s.label} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{ fontSize: 18, fontWeight: 700, color: s.color, fontFamily: 'JetBrains Mono, monospace' }}>
              {s.value}
            </span>
            <span style={{ fontSize: 11, color: '#d4d6d7ff' }}>{s.label}</span>
          </div>
        ))}
        <div style={{ flex: 1 }} />
        <button
          onClick={() => setPaused(p => !p)}
          style={{
            background: paused ? '#00FF9F22' : '#FF3B5C22',
            border:     `1px solid ${paused ? '#00FF9F44' : '#FF3B5C44'}`,
            color:      paused ? '#00FF9F' : '#FF3B5C',
            borderRadius: 6, padding: '4px 12px', cursor: 'pointer', fontSize: 12, fontWeight: 600,
          }}
        >
          {paused ? '▶ Resume' : '⏸ Pause'}
        </button>
      </div>

      {/* Filter pills */}
      <div style={{ padding: '8px 20px', borderBottom: '1px solid #0D2244', display: 'flex', gap: 6, flexShrink: 0, flexWrap: 'wrap' }}>
        {FILTER_TYPES.map(f => {
          const color = f.id === 'all' ? '#00D4FF' : TYPE_COLORS[f.id as EBPFEventType] ?? '#00D4FF';
          return (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              style={{
                border:     filter === f.id ? `1px solid ${color}66` : '1px solid #0D2244',
                background: filter === f.id ? `${color}15` : 'transparent',
                color:      filter === f.id ? color : '#c6c7c8ff',
                borderRadius: 6, padding: '3px 10px', cursor: 'pointer', fontSize: 11,
                transition: 'all 0.15s',
              }}
            >
              {f.id !== 'all' && TYPE_ICONS[f.id as EBPFEventType]} {f.label}
            </button>
          );
        })}
      </div>

      {/* Main layout */}
      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>

        {/* Event stream */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '8px 16px' }}>
          {events.map((evt, i) => {
            const tc    = TYPE_COLORS[evt.type];
            const sc    = SEV_COLORS[evt.severity];
            const isNew = i === 0 && !paused;
            return (
              <div
                key={evt.id}
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  gap: 10,
                  padding: "6px 8px",
                  marginBottom: 3,
                  borderRadius: 6,
                  background: isNew ? "#00D4FF08" : "transparent",
                  border: `1px solid ${evt.severity === "critical" ? "#FF3B5C22" : "transparent"}`,
                  transition: "background 0.4s",
                  animation: isNew ? "fadeSlideIn 0.2s ease" : undefined,
                }}
              >
                {/* Severity dot */}
                <div
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: "50%",
                    background: sc,
                    boxShadow:
                      evt.severity === "critical" ? `0 0 6px ${sc}` : "none",
                    marginTop: 5,
                    flexShrink: 0,
                  }}
                />

                {/* Timestamp */}
                <span
                  style={{
                    fontSize: 10,
                    color: "#e6acfaff",
                    fontFamily: "JetBrains Mono, monospace",
                    minWidth: 90,
                    marginTop: 2,
                    flexShrink: 0,
                  }}
                >
                  {evt.ts}
                </span>

                {/* Type badge */}
                <span
                  style={{
                    fontSize: 10,
                    fontFamily: "JetBrains Mono, monospace",
                    fontWeight: 700,
                    color: tc,
                    background: `${tc}15`,
                    border: `1px solid ${tc}33`,
                    borderRadius: 4,
                    padding: "1px 6px",
                    flexShrink: 0,
                  }}
                >
                  {TYPE_ICONS[evt.type]} {evt.type}
                </span>

                {/* Pod */}
                <span
                  style={{
                    fontSize: 10,
                    color: "#00D4FF",
                    fontFamily: "JetBrains Mono, monospace",
                    minWidth: 100,
                    flexShrink: 0,
                  }}
                >
                  {evt.pod}
                </span>

                {/* Detail */}
                <span
                  style={{
                    fontSize: 11,
                    color: "#e6acfaff",
                    flex: 1,
                    lineHeight: 1.4,
                    fontFamily: "JetBrains Mono, monospace",
                    minWidth: 0,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {evt.detail}
                </span>

                {/* Node */}
                <span
                  style={{
                    fontSize: 9,
                    color: "#f1dbf8ff",
                    fontFamily: "JetBrains Mono, monospace",
                    flexShrink: 0,
                  }}
                >
                  {evt.node}
                </span>
              </div>
            );
          })}
        </div>

        {/* Probe health panel */}
        <div style={{ width: 220, flexShrink: 0, borderLeft: '1px solid #0D2244', padding: 14, overflowY: 'auto' }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: '#C8D8E8', marginBottom: 12 }}>
            Probe Health
          </div>
          {PROBES.map(p => (
            <div key={p.name} style={{
              background: '#06111F', border: '1px solid #0D2244',
              borderRadius: 8, padding: '8px 10px', marginBottom: 8,
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                <span style={{ fontSize: 10, color: '#00D4FF', fontFamily: 'JetBrains Mono, monospace' }}>{p.name}</span>
                <GlowBadge severity="info" label="ACTIVE" />
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ fontSize: 9, color: '#c9cacaff' }}>events</span>
                <span style={{ fontSize: 9, color: '#FFB800', fontFamily: 'JetBrains Mono, monospace' }}>{p.events.toLocaleString()}</span>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ fontSize: 9, color: '#b1b2b2ff' }}>overhead</span>
                <span style={{ fontSize: 9, color: '#00FF9F', fontFamily: 'JetBrains Mono, monospace' }}>{p.overhead}</span>
              </div>
            </div>
          ))}
          <div style={{ marginTop: 4, padding: '6px 10px', background: '#040C1A', borderRadius: 6, border: '1px solid #0D2244' }}>
            <div style={{ fontSize: 10, color: '#9dccfcff' }}>Total eBPF overhead</div>
            <div style={{ fontSize: 14, fontWeight: 700, color: '#00FF9F', fontFamily: 'JetBrains Mono, monospace' }}>0.71%</div>
            <div style={{ fontSize: 9, color: '#9bcbfaff', marginTop: 2 }}>Target &lt;1%</div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default EBPFTab;

import React from 'react';
import { useIncidentStore } from '@/stores/useIncidentStore';
import { GlowBadge }        from '@/components/shared/GlowBadge';
import { MetricBar }        from '@/components/shared/MetricBar';
import {
  AlertCircle,
  AlertTriangle,
  Brain,
  Shield,
  Sparkles,
  CheckCircle,
  Zap,
  TrendingDown,
  Clock,
} from "lucide-react";

import type { Incident, IncidentStatus } from '@/types';

const STATUS_FILTERS: { id: string; label: string }[] = [
  { id: 'all',          label: 'All'          },
  { id: 'active',       label: 'Active'       },
  { id: 'investigating',label: 'Investigating' },
  { id: 'auto-resolved',label: 'Auto-Resolved' },
  { id: 'resolved',     label: 'Resolved'     },
];
const ICON_MAP: Record<string, React.ReactNode> = {
  critical: <AlertCircle size={14} color="#FF3B5C" />,
  warning: <AlertTriangle size={14} color="#FBBF24" />,
  brain: <Brain size={14} color="#A78BFA" />,
  shield: <Shield size={14} color="#00D4FF" />,
  ghost: <Sparkles size={14} color="#A78BFA" />,
  success: <CheckCircle size={14} color="#00FF9F" />,
  zap: <Zap size={14} color="#FF3B5C" />,
  trend_down: <TrendingDown size={14} color="#34D399" />,
};
const STATUS_COLORS: Record<IncidentStatus, string> = {
  'active':        '#FF3B5C',
  'investigating': '#FFB800',
  'auto-resolved': '#00FF9F',
  'resolved':      '#4A6A8A',
};

function IncidentListItem({
  inc,
  selected,
  onClick,
}: {
  inc:      Incident;
  selected: boolean;
  onClick:  () => void;
}) {
  const sc = STATUS_COLORS[inc.status];
  return (
    <div
      onClick={onClick}
      style={{
        padding: "12px 14px",
        marginBottom: 6,
        borderRadius: 8,
        background: selected ? "#06111F" : "#04091A",
        border: `1px solid ${selected ? "#00D4FF44" : "#0D2244"}`,
        cursor: "pointer",
        transition: "all 0.15s",
      }}
      onMouseEnter={(e) => {
        if (!selected)
          (e.currentTarget as HTMLElement).style.borderColor = "#1A3A6A";
      }}
      onMouseLeave={(e) => {
        if (!selected)
          (e.currentTarget as HTMLElement).style.borderColor = "#0D2244";
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
          style={{
            fontSize: 11,
            fontWeight: 700,
            color: "#00D4FF",
            fontFamily: "JetBrains Mono, monospace",
          }}
        >
          {inc.id}
        </span>
        <GlowBadge
          severity={inc.severity === "info" ? "info" : inc.severity}
          label={inc.status}
        />
      </div>
      <div
        style={{
          fontSize: 12,
          color: "#C8D8E8",
          marginBottom: 6,
          lineHeight: 1.4,
        }}
      >
        {inc.title}
      </div>
      <div style={{ display: "flex", justifyContent: "space-between" }}>
        <span
          style={{
            display: "flex",
            alignItems: "center",
            gap: 4,
            fontSize: 10,
            color: "#4A6A8A",
            fontFamily: "JetBrains Mono, monospace",
          }}
        >
          <Clock size={12} color="#00FF9F" />
          {inc.elapsed}
        </span>
        <span
          style={{
            fontSize: 10,
            color: sc,
            fontFamily: "JetBrains Mono, monospace",
          }}
        >
          {inc.node}
        </span>
      </div>
    </div>
  );
}

function IncidentDetail({ inc }: { inc: Incident }) {
  const sc = STATUS_COLORS[inc.status];

  return (
    <div style={{ height: "100%", overflowY: "auto", padding: 20 }}>
      {/* Header */}
      <div style={{ marginBottom: 20 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            marginBottom: 8,
          }}
        >
          <span
            style={{
              fontSize: 13,
              fontWeight: 700,
              color: "#00D4FF",
              fontFamily: "JetBrains Mono, monospace",
            }}
          >
            {inc.id}
          </span>
          <GlowBadge
            severity={inc.severity === "info" ? "info" : inc.severity}
            label={inc.severity.toUpperCase()}
          />
          <GlowBadge
            severity={inc.type === "attack" ? "critical" : "warning"}
            label={inc.type.toUpperCase()}
          />
        </div>
        <div
          style={{
            fontSize: 16,
            fontWeight: 600,
            color: "#C8D8E8",
            marginBottom: 4,
          }}
        >
          {inc.title}
        </div>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
          {[
            { label: "Opened", value: inc.opened },
            { label: "Elapsed", value: inc.elapsed },
            { label: "MTTR Target", value: inc.mttrTarget },
            { label: "Status", value: inc.status, color: sc },
          ].map((kv) => (
            <div key={kv.label} style={{ display: "flex", gap: 5 }}>
              <span style={{ fontSize: 11, color: "#4A6A8A" }}>
                {kv.label}:
              </span>
              <span
                style={{
                  fontSize: 11,
                  color: (kv as { color?: string }).color ?? "#C8D8E8",
                  fontFamily: "JetBrains Mono, monospace",
                }}
              >
                {kv.value}
              </span>
            </div>
          ))}
        </div>
      </div>

      {/* Root cause */}
      <div
        style={{
          background: "#06111F",
          border: "1px solid #0D2244",
          borderRadius: 10,
          padding: 16,
          marginBottom: 16,
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            fontSize: 11,
            color: "#94A3B8",
            textTransform: "uppercase",
            letterSpacing: 0.5,
            marginBottom: 8,
          }}
        >
          <Brain size={14} color="#A78BFA" />
          <span>Root Cause (Causal GNN)</span>
        </div>
        <div style={{ fontSize: 12, color: "#C8D8E8", lineHeight: 1.6 }}>
          {inc.rootCause}
        </div>
        <div style={{ marginTop: 10 }}>
          <MetricBar
            label="GNN Confidence"
            value={Math.round(inc.confidence)}
            color="#9B5DE5"
            height={6}
          />
        </div>
      </div>

      {/* Affected services */}
      <div
        style={{
          background: "#06111F",
          border: "1px solid #0D2244",
          borderRadius: 10,
          padding: 16,
          marginBottom: 16,
        }}
      >
        <div
          style={{
            fontSize: 11,
            color: "#4A6A8A",
            textTransform: "uppercase",
            letterSpacing: 0.5,
            marginBottom: 10,
          }}
        >
          Affected Services ({inc.affected.length})
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {inc.affected.map((s) => (
            <span
              key={s}
              style={{
                background: "#FF3B5C15",
                border: "1px solid #FF3B5C44",
                borderRadius: 5,
                padding: "3px 10px",
                fontSize: 11,
                color: "#FF3B5C",
                fontFamily: "JetBrains Mono, monospace",
              }}
            >
              {s}
            </span>
          ))}
        </div>
      </div>

      {/* Autonomous action */}
      <div
        style={{
          background: "#06111F",
          border: "1px solid #00FF9F33",
          borderRadius: 10,
          padding: 16,
          marginBottom: 16,
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            fontSize: 11,
            color: "#4A6A8A",
            textTransform: "uppercase",
            letterSpacing: 0.5,
            marginBottom: 8,
          }}
        >
          <Zap size={14} color="#22D3EE" />
          <span>Autonomous Action</span>
        </div>
        <div style={{ fontSize: 12, color: "#00FF9F", lineHeight: 1.6 }}>
          {inc.autoAction}
        </div>
      </div>

      {/* Timeline */}
      <div
        style={{
          background: "#06111F",
          border: "1px solid #0D2244",
          borderRadius: 10,
          padding: 16,
        }}
      >
        <div
          style={{
            fontSize: 11,
            color: "#4A6A8A",
            textTransform: "uppercase",
            letterSpacing: 0.5,
            marginBottom: 14,
          }}
        >
          Timeline
        </div>
        {inc.timeline.map((evt, i) => (
          <div
            key={i}
            style={{
              display: "flex",
              gap: 12,
              marginBottom: 12,
              alignItems: "flex-start",
            }}
          >
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                flexShrink: 0,
              }}
            >
              <div style={{ fontSize: 16 }}>
                {ICON_MAP[evt.icon] ?? evt.icon}
              </div>
              {i < inc.timeline.length - 1 && (
                <div
                  style={{
                    width: 1,
                    height: 14,
                    background: "#0D2244",
                    marginTop: 3,
                  }}
                />
              )}
            </div>
            <div style={{ paddingTop: 1 }}>
              <div
                style={{
                  fontSize: 10,
                  color: "#4A6A8A",
                  fontFamily: "JetBrains Mono, monospace",
                  marginBottom: 2,
                }}
              >
                {evt.time}
              </div>
              <div style={{ fontSize: 12, color: "#C8D8E8", lineHeight: 1.4 }}>
                {evt.event}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export const IncidentsTab: React.FC = () => {
  const { incidents, selected, setSelected, statusFilter, setFilter } = useIncidentStore();

  const filtered = incidents.filter(
    inc => statusFilter === 'all' || inc.status === statusFilter,
  );

  return (
    <div style={{ height: '100%', display: 'flex', overflow: 'hidden' }}>

      {/* Left: Incident list */}
      <div style={{ width: 300, flexShrink: 0, borderRight: '1px solid #0D2244', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>

        {/* Filter pills */}
        <div style={{ padding: '10px 12px', borderBottom: '1px solid #0D2244', flexShrink: 0 }}>
          <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
            {STATUS_FILTERS.map(f => (
              <button
                key={f.id}
                onClick={() => setFilter(f.id)}
                style={{
                  border:     statusFilter === f.id ? '1px solid #00D4FF66' : '1px solid #0D2244',
                  background: statusFilter === f.id ? '#00D4FF15' : 'transparent',
                  color:      statusFilter === f.id ? '#00D4FF' : '#4A6A8A',
                  borderRadius: 5, padding: '3px 8px', cursor: 'pointer', fontSize: 10,
                }}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>

        {/* List */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '10px 12px' }}>
          {filtered.map(inc => (
            <IncidentListItem
              key={inc.id}
              inc={inc}
              selected={selected?.id === inc.id}
              onClick={() => setSelected(inc)}
            />
          ))}
        </div>
      </div>

      {/* Right: Detail */}
      <div style={{ flex: 1, overflow: 'hidden', background: '#040C1A' }}>
        {selected ? (
          <IncidentDetail inc={selected} />
        ) : (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: '#4A6A8A', fontSize: 13 }}>
            Select an incident to view details
          </div>
        )}
      </div>
    </div>
  );
};

export default IncidentsTab;

import React, { useState } from 'react';
import { GhostPreviewModal } from '@/components/ghost/GhostPreviewModal';
import { MetricBar } from '@/components/shared/MetricBar';
import { GlowBadge } from '@/components/shared/GlowBadge';
import { Ghost, Shield, Zap, Settings } from "lucide-react";
import type { GhostAction } from '@/types';
import { useGuardian } from '@/hooks/useGuardian';

interface Props {
  onOpenGhost?: (action: GhostAction) => void;
}

const RISK_COLORS = { LOW: '#00FF9F', MED: '#FFB800', HIGH: '#FF3B5C' };

export const GuardianTab: React.FC<Props> = () => {
  const [ghostAction, setGhostAction] = useState<GhostAction | null>(null);
  const { data, loading, error } = useGuardian();

  if (loading || !data) {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          height: "100%",
          color: "#4A6A8A",
        }}
      >
        <div style={{ textAlign: "center" }}>
          <div
            style={{
              fontSize: 32,
              marginBottom: 8,
              animation: "spin 1s linear infinite",
            }}
          >
            <Settings size={32} color="#22D3EE" />
          </div>
          <div>{error ? `Error: ${error}` : 'Loading Guardian…'}</div>
        </div>
      </div>
    );
  }

  const KPI_ITEMS = [
    { label: 'MTTR Reduction',   value: data.kpis.mttrReduction,   color: '#00FF9F', target: '>50%'  },
    { label: 'False Positive',   value: data.kpis.falsePositive,   color: '#00D4FF', target: '<5%'   },
    { label: 'OPA Compliance',   value: data.kpis.opaCompliance,   color: '#9B5DE5', target: '100%'  },
    { label: 'Auto-Resolved',    value: data.kpis.autoResolved,    color: '#FFB800', target: '>70%'  },
  ];

  return (
    <div style={{ height: "100%", overflowY: "auto", padding: 20 }}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        {/* KPIs */}
        <div
          style={{
            gridColumn: "1 / -1",
            display: "grid",
            gridTemplateColumns: "repeat(4, 1fr)",
            gap: 12,
          }}
        >
          {KPI_ITEMS.map((k) => (
            <div
              key={k.label}
              style={{
                background: "#06111F",
                border: "1px solid #0D2244",
                borderRadius: 10,
                padding: "14px 16px",
              }}
            >
              <div
                style={{
                  fontSize: 28,
                  fontWeight: 700,
                  color: k.color,
                  fontFamily: "JetBrains Mono, monospace",
                }}
              >
                {k.value}
              </div>
              <div style={{ fontSize: 12, color: "#dadbdcff", marginTop: 4 }}>
                {k.label}
              </div>
              <div style={{ fontSize: 10, color: "#dbeaf9ff", marginTop: 2 }}>
                Target: {k.target}
              </div>
            </div>
          ))}
        </div>

        {/* OPA Policies */}
        <div
          style={{
            background: "#06111F",
            border: "1px solid #0D2244",
            borderRadius: 12,
            padding: 20,
          }}
        >
          <div
            style={{
              fontSize: 12,
              fontWeight: 600,
              color: "#ffffffff",
              marginBottom: 16,
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <Shield size={14} color="#9B5DE5" />
            <span>OPA Policy Engine</span>
            <span
              style={{
                fontSize: 10,
                color: "#00FF9F",
                marginLeft: 8,
                fontFamily: "JetBrains Mono, monospace",
              }}
            >
              {data.policies.filter((p) => p.violations === 0).length}/
              {data.policies.length} PASS
            </span>
          </div>
          {data.policies.map((p) => {
            const hasViol = p.violations > 0;
            return (
              <div
                key={p.id}
                style={{
                  padding: "10px 12px",
                  marginBottom: 8,
                  borderRadius: 8,
                  background: "#040C1A",
                  border: `1px solid ${hasViol ? "#FF3B5C33" : "#0D2244"}`,
                }}
              >
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    marginBottom: 3,
                  }}
                >
                  <span
                    style={{
                      fontSize: 11,
                      color: "#00D4FF",
                      fontFamily: "JetBrains Mono, monospace",
                    }}
                  >
                    {p.name}
                  </span>
                  <GlowBadge
                    severity={hasViol ? "critical" : "info"}
                    label={hasViol ? `${p.violations} VIOLATION` : "PASS"}
                  />
                </div>
                <div
                  style={{ fontSize: 11, color: "#fafcfcff", lineHeight: 1.4 }}
                >
                  {p.description}
                </div>
              </div>
            );
          })}
        </div>

        {/* RL Actions */}
        <div
          style={{
            background: "#06111F",
            border: "1px solid #0D2244",
            borderRadius: 12,
            padding: 20,
          }}
        >
          <div
            style={{
              fontSize: 12,
              fontWeight: 600,
              color: "#f9faf9ff",
              marginBottom: 16,
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <Zap size={14} color="#FF8C00" />
            <span>RL-Proposed Remediations</span>
          </div>
          {data.actions.map((a) => {
            const riskColor = RISK_COLORS[a.risk];
            return (
              <div
                key={a.id}
                style={{
                  padding: "10px 12px",
                  marginBottom: 10,
                  borderRadius: 8,
                  background: "#040C1A",
                  border: "1px solid #0D2244",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    marginBottom: 6,
                  }}
                >
                  <div style={{ display: "flex", gap: 6 }}>
                    <GlowBadge
                      severity={
                        a.risk === "LOW"
                          ? "info"
                          : a.risk === "MED"
                            ? "warning"
                            : "critical"
                      }
                      label={`RISK ${a.risk}`}
                    />
                    <GlowBadge severity="info" label={a.impact} />
                  </div>
                  <span
                    style={{
                      fontSize: 12,
                      fontWeight: 700,
                      color: riskColor,
                      fontFamily: "JetBrains Mono, monospace",
                    }}
                  >
                    {a.confidence.toFixed(1)}%
                  </span>
                </div>
                <div
                  style={{
                    fontSize: 11,
                    color: "#f7f9fcff",
                    lineHeight: 1.4,
                    marginBottom: 8,
                  }}
                >
                  {a.action}
                </div>
                <div style={{ marginBottom: 8 }}>
                  <MetricBar
                    label="Confidence"
                    value={Math.round(a.confidence)}
                    color={riskColor}
                    height={4}
                  />
                </div>
                <button
                  onClick={() =>
                    setGhostAction({
                      label: a.action.slice(0, 30) + "…",
                      icon: <Ghost size={16} />,
                      actionName: a.actionName,
                      targetNode: a.targetNode,
                    })
                  }
                  style={{
                    width: "100%",
                    padding: "6px 10px",
                    borderRadius: 6,
                    cursor: "pointer",
                    background: "#9B5DE515",
                    border: "1px solid #9B5DE544",
                    color: "#ffffff",
                    fontSize: 11,
                    fontWeight: 600,
                    transition: "background 0.15s",
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                  }}
                  onMouseEnter={(e) =>
                    (e.currentTarget.style.background = "#9B5DE525")
                  }
                  onMouseLeave={(e) =>
                    (e.currentTarget.style.background = "#9B5DE515")
                  }
                >
                  <Ghost size={14} color="#ffffff" />
                  <span>Ghost Preview</span>
                </button>
              </div>
            );
          })}
        </div>
      </div>

      {ghostAction && (
        <GhostPreviewModal
          action={ghostAction}
          onClose={() => setGhostAction(null)}
        />
      )}
    </div>
  );
};

export default GuardianTab;

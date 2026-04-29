import React from 'react';
import { useGNN } from '@/hooks/useGNN';
import { MetricBar } from '@/components/shared/MetricBar';
import { GlowBadge } from '@/components/shared/GlowBadge';
import { Settings } from "lucide-react";

const CLASS_COLORS = { healthy: '#00FF9F', fault: '#FFB800', attack: '#FF3B5C' };

const CAUSAL_SIGNAL = [
  { step: '1', node: 'order-svc',   event: 'CAP_SYS_ADMIN capability acquired (eBPF)', type: 'attack' as const },
  { step: '2', node: 'order-svc',   event: 'Syscall rate spike: 8,400/s (2.1σ above baseline)', type: 'attack' as const },
  { step: '3', node: 'postgres',    event: 'Memory pressure rising: RSS 3.8GB / 4GB limit', type: 'fault' as const },
  { step: '4', node: 'postgres',    event: 'OOM Kill — kernel evicted postgres process', type: 'fault' as const },
  { step: '5', node: 'order-svc',   event: 'TCP retransmit storm: 187 retransmits/s to postgres', type: 'fault' as const },
  { step: '6', node: 'notify-svc',  event: 'Scheduler latency p99=142ms due to CPU contention', type: 'fault' as const },
];

export const IntelligenceTab: React.FC = () => {
  const { inference, loading } = useGNN();

  if (loading) {
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
          <div>Running GNN inference…</div>
        </div>
      </div>
    );
  }

  const graphProbs   = inference.graphClassification;
  const dominant     = Object.entries(graphProbs).sort((a, b) => b[1] - a[1])[0];
  const domColor     = CLASS_COLORS[dominant[0] as keyof typeof CLASS_COLORS];

  return (
    <div style={{ height: '100%', overflowY: 'auto', padding: 20 }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>

        {/* Graph classification */}
        <div style={{
          background: '#06111F', border: '1px solid #0D2244', borderRadius: 12, padding: 20,
        }}>
          <div style={{ fontSize: 11, color: '#ecb5fbff', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 14 }}>
            Graph-Level Classification
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 16 }}>
            <div style={{
              width: 64, height: 64, borderRadius: '50%',
              background: `${domColor}22`, border: `3px solid ${domColor}`,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              boxShadow: `0 0 24px ${domColor}44`, flexShrink: 0,
            }}>
              <span style={{ fontSize: 22, fontWeight: 700, color: domColor, fontFamily: 'JetBrains Mono, monospace' }}>
                {(dominant[1] * 100).toFixed(0)}%
              </span>
            </div>
            <div>
              <div style={{ fontSize: 20, fontWeight: 700, color: domColor, textTransform: 'uppercase' }}>
                {dominant[0]}
              </div>
              <div style={{ fontSize: 11, color: '#4A6A8A', marginTop: 2 }}>
                Inference: {inference.inferenceMs}ms
              </div>
            </div>
          </div>
          {Object.entries(graphProbs).map(([cls, prob]) => (
            <div key={cls} style={{ marginBottom: 10 }}>
              <MetricBar
                label={cls.toUpperCase()}
                value={Math.round(prob * 100)}
                color={CLASS_COLORS[cls as keyof typeof CLASS_COLORS]}
                height={6}
              />
            </div>
          ))}
        </div>

        {/* Root cause */}
        <div style={{
          background: '#06111F', border: '1px solid #0D2244', borderRadius: 12, padding: 20,
        }}>
          <div style={{ fontSize: 11, color: '#a1baf9ff', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 14 }}>
            Root Cause Analysis
          </div>

          <div style={{
            background: '#FF3B5C11', border: '1px solid #FF3B5C44', borderRadius: 8,
            padding: '12px 14px', marginBottom: 16,
          }}>
            <div style={{ fontSize: 11, color: '#f1f7fdff', marginBottom: 4 }}>Root Cause Node</div>
            <div style={{ fontSize: 18, fontWeight: 700, color: '#FF3B5C', fontFamily: 'JetBrains Mono, monospace' }}>
              {inference.rootCauseNode}
            </div>
            <div style={{ marginTop: 8 }}>
              <MetricBar label="Confidence" value={Math.round(inference.rootCauseConfidence * 100)} color="#FF3B5C" height={6} />
            </div>
          </div>

          <div style={{ fontSize: 11, color: '#daeafaff', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 8 }}>
            Blast Radius ({inference.blastRadius.length} nodes)
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {inference.blastRadius.map(n => (
              <span key={n} style={{
                background: '#FF3B5C15', border: '1px solid #FF3B5C44',
                borderRadius: 5, padding: '2px 8px',
                fontSize: 11, color: '#FF3B5C', fontFamily: 'JetBrains Mono, monospace',
              }}>
                {n}
              </span>
            ))}
          </div>
        </div>

        {/* Causal chain */}
        <div style={{
          background: '#06111F', border: '1px solid #0D2244', borderRadius: 12, padding: 20,
          gridColumn: '1 / -1',
        }}>
          <div style={{ fontSize: 11, color: '#a3edf9ff', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 14 }}>
            Causal Chain (Pearl's do-calculus — eBPF evidence)
          </div>
          {CAUSAL_SIGNAL.map((step, i) => {
            const isAttack = step.type === 'attack';
            const stepColor = isAttack ? '#FF3B5C' : '#FFB800';
            return (
              <div key={i} style={{ display: 'flex', gap: 12, marginBottom: 12, alignItems: 'flex-start' }}>
                {/* Step circle + connector */}
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', flexShrink: 0 }}>
                  <div style={{
                    width: 28, height: 28, borderRadius: '50%',
                    background: `${stepColor}22`, border: `2px solid ${stepColor}`,
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: 11, fontWeight: 700, color: stepColor,
                    fontFamily: 'JetBrains Mono, monospace',
                  }}>
                    {step.step}
                  </div>
                  {i < CAUSAL_SIGNAL.length - 1 && (
                    <div style={{ width: 2, height: 16, background: '#0D2244', marginTop: 2 }} />
                  )}
                </div>
                {/* Content */}
                <div style={{ flex: 1, paddingTop: 4 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 3 }}>
                    <span style={{ fontSize: 11, fontWeight: 600, color: '#00D4FF', fontFamily: 'JetBrains Mono, monospace' }}>
                      {step.node}
                    </span>
                    <GlowBadge severity={isAttack ? 'critical' : 'warning'} label={step.type} />
                  </div>
                  <div style={{ fontSize: 12, color: '#fbf1feff', lineHeight: 1.4 }}>{step.event}</div>
                </div>
              </div>
            );
          })}
        </div>

        {/* Node classifications */}
        <div style={{
          background: '#06111F', border: '1px solid #0D2244', borderRadius: 12, padding: 20,
          gridColumn: '1 / -1',
        }}>
          <div style={{ fontSize: 11, color: '#dbe8fbff', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 14 }}>
            Per-Node Classification (GNN Output)
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 10 }}>
            {Object.entries(inference.nodeClassifications).map(([nodeName, probs]) => {
              const dom   = Object.entries(probs).sort((a, b) => b[1] - a[1])[0];
              const color = CLASS_COLORS[dom[0] as keyof typeof CLASS_COLORS];
              return (
                <div key={nodeName} style={{
                  background: '#040C1A', borderRadius: 8, padding: '10px 12px',
                  border: `1px solid ${color}33`,
                }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                    <span style={{ fontSize: 11, color: '#00D4FF', fontFamily: 'JetBrains Mono, monospace' }}>{nodeName}</span>
                    <GlowBadge
                      severity={dom[0] === 'healthy' ? 'info' : dom[0] === 'fault' ? 'warning' : 'critical'}
                      label={`${(dom[1] * 100).toFixed(0)}%`}
                    />
                  </div>
                  {Object.entries(probs).map(([cls, prob]) => (
                    <div key={cls} style={{ marginBottom: 5 }}>
                      <MetricBar label={cls} value={Math.round(prob * 100)} color={CLASS_COLORS[cls as keyof typeof CLASS_COLORS]} height={4} />
                    </div>
                  ))}
                </div>
              );
            })}
          </div>
        </div>

      </div>
    </div>
  );
};

export default IntelligenceTab;

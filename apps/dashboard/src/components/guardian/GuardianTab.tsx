import React, { useState } from 'react';
import { GhostPreviewModal } from '@/components/ghost/GhostPreviewModal';
import { MetricBar } from '@/components/shared/MetricBar';
import { GlowBadge } from '@/components/shared/GlowBadge';
import type { GhostAction, OPAPolicy, RLAction } from '@/types';

interface Props {
  onOpenGhost: (action: GhostAction) => void;
}

const POLICIES: OPAPolicy[] = [
  { id: 'p1', name: 'no_privilege_escalation', status: 'active', violations: 1, description: 'Block CAP_SYS_ADMIN acquisition from non-root processes' },
  { id: 'p2', name: 'cpu_threshold',           status: 'active', violations: 0, description: 'Prevent scale-down below 2 replicas when CPU >70%' },
  { id: 'p3', name: 'egress_control',          status: 'active', violations: 0, description: 'Block egress to non-allowlisted CIDR ranges' },
  { id: 'p4', name: 'lateral_movement',        status: 'active', violations: 1, description: 'Deny cross-namespace pod access patterns' },
  { id: 'p5', name: 'oom_notification',        status: 'active', violations: 0, description: 'Require SRE notification for OOM-risk pod actions' },
];

const RL_ACTIONS: RLAction[] = [
  { id: 1, action: 'Isolate order-svc container — block all ingress/egress', confidence: 94.2, risk: 'LOW', impact: 'MTTR -65%', actionName: 'isolate_container',   targetNode: 'order-svc'  },
  { id: 2, action: 'Apply deny-all NetworkPolicy to lateral movement paths',  confidence: 87.1, risk: 'LOW', impact: 'MTTR -50%', actionName: 'apply_network_policy', targetNode: 'order-svc'  },
  { id: 3, action: 'Block outbound IP 203.0.113.47 (C2 candidate)',           confidence: 79.3, risk: 'LOW', impact: 'MTTR -40%', actionName: 'block_ip',            targetNode: 'order-svc'  },
  { id: 4, action: 'Increase postgres memory limit from 4GB → 6GB',          confidence: 71.8, risk: 'MED', impact: 'MTTR -45%', actionName: 'increase_memory_limit',targetNode: 'postgres'   },
  { id: 5, action: 'Scale up notify-svc replicas (1 → 3)',                   confidence: 68.4, risk: 'LOW', impact: 'MTTR -30%', actionName: 'scale_up_replicas',   targetNode: 'notify-svc' },
];

const KPI_ITEMS = [
  { label: 'MTTR Reduction',   value: '68%',  color: '#00FF9F', target: '>50%'  },
  { label: 'False Positive',   value: '2.1%', color: '#00D4FF', target: '<5%'   },
  { label: 'OPA Compliance',   value: '100%', color: '#9B5DE5', target: '100%'  },
  { label: 'Auto-Resolved',    value: '71%',  color: '#FFB800', target: '>70%'  },
];

const RISK_COLORS = { LOW: '#00FF9F', MED: '#FFB800', HIGH: '#FF3B5C' };

export const GuardianTab: React.FC<Props> = () => {
  const [ghostAction, setGhostAction] = useState<GhostAction | null>(null);

  return (
    <div style={{ height: '100%', overflowY: 'auto', padding: 20 }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>

        {/* KPIs */}
        <div style={{ gridColumn: '1 / -1', display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12 }}>
          {KPI_ITEMS.map(k => (
            <div key={k.label} style={{
              background: '#06111F', border: '1px solid #0D2244', borderRadius: 10, padding: '14px 16px',
            }}>
              <div style={{ fontSize: 28, fontWeight: 700, color: k.color, fontFamily: 'JetBrains Mono, monospace' }}>
                {k.value}
              </div>
              <div style={{ fontSize: 12, color: '#8899AA', marginTop: 4 }}>{k.label}</div>
              <div style={{ fontSize: 10, color: '#4A6A8A', marginTop: 2 }}>Target: {k.target}</div>
            </div>
          ))}
        </div>

        {/* OPA Policies */}
        <div style={{ background: '#06111F', border: '1px solid #0D2244', borderRadius: 12, padding: 20 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: '#C8D8E8', marginBottom: 16 }}>
            🛡 OPA Policy Engine
            <span style={{ fontSize: 10, color: '#00FF9F', marginLeft: 8, fontFamily: 'JetBrains Mono, monospace' }}>
              {POLICIES.filter(p => p.violations === 0).length}/{POLICIES.length} PASS
            </span>
          </div>
          {POLICIES.map(p => {
            const hasViol = p.violations > 0;
            return (
              <div key={p.id} style={{
                padding: '10px 12px', marginBottom: 8, borderRadius: 8,
                background: '#040C1A',
                border: `1px solid ${hasViol ? '#FF3B5C33' : '#0D2244'}`,
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 3 }}>
                  <span style={{ fontSize: 11, color: '#00D4FF', fontFamily: 'JetBrains Mono, monospace' }}>
                    {p.name}
                  </span>
                  <GlowBadge severity={hasViol ? 'critical' : 'info'} label={hasViol ? `${p.violations} VIOLATION` : 'PASS'} />
                </div>
                <div style={{ fontSize: 11, color: '#4A6A8A', lineHeight: 1.4 }}>{p.description}</div>
              </div>
            );
          })}
        </div>

        {/* RL Actions */}
        <div style={{ background: '#06111F', border: '1px solid #0D2244', borderRadius: 12, padding: 20 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: '#C8D8E8', marginBottom: 16 }}>
            ⚡ RL-Proposed Remediations
          </div>
          {RL_ACTIONS.map(a => {
            const riskColor = RISK_COLORS[a.risk];
            return (
              <div key={a.id} style={{
                padding: '10px 12px', marginBottom: 10, borderRadius: 8,
                background: '#040C1A', border: '1px solid #0D2244',
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                  <div style={{ display: 'flex', gap: 6 }}>
                    <GlowBadge severity={a.risk === 'LOW' ? 'info' : a.risk === 'MED' ? 'warning' : 'critical'} label={`RISK ${a.risk}`} />
                    <GlowBadge severity="info" label={a.impact} />
                  </div>
                  <span style={{ fontSize: 12, fontWeight: 700, color: riskColor, fontFamily: 'JetBrains Mono, monospace' }}>
                    {a.confidence.toFixed(1)}%
                  </span>
                </div>
                <div style={{ fontSize: 11, color: '#8899AA', lineHeight: 1.4, marginBottom: 8 }}>{a.action}</div>
                <div style={{ marginBottom: 8 }}>
                  <MetricBar label="Confidence" value={Math.round(a.confidence)} color={riskColor} height={4} />
                </div>
                <button
                  onClick={() => setGhostAction({ label: a.action.slice(0, 30) + '…', icon: '👻', actionName: a.actionName, targetNode: a.targetNode })}
                  style={{
                    width: '100%', padding: '6px 10px', borderRadius: 6, cursor: 'pointer',
                    background: '#9B5DE515', border: '1px solid #9B5DE544', color: '#9B5DE5',
                    fontSize: 11, fontWeight: 600, transition: 'background 0.15s',
                  }}
                  onMouseEnter={e => (e.currentTarget.style.background = '#9B5DE525')}
                  onMouseLeave={e => (e.currentTarget.style.background = '#9B5DE515')}
                >
                  👻 Ghost Preview
                </button>
              </div>
            );
          })}
        </div>

      </div>

      {ghostAction && (
        <GhostPreviewModal action={ghostAction} onClose={() => setGhostAction(null)} />
      )}
    </div>
  );
};

export default GuardianTab;

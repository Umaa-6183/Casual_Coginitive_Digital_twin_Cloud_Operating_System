import React from 'react';
import type { ServiceNode, GhostAction } from '@/types';
import { MetricBar }  from '@/components/shared/MetricBar';
import { GlowBadge }  from '@/components/shared/GlowBadge';

interface Props {
  node:          ServiceNode;
  onClose:       () => void;
  onGhostPreview:(action: GhostAction) => void;
}

const LAYER_LABELS: Record<string, string> = {
  network: 'NETWORK',
  service: 'SERVICE',
  data:    'DATA',
  system:  'SYSTEM',
};

export const NodeCard: React.FC<Props> = ({ node, onClose, onGhostPreview }) => {
  const actions: GhostAction[] = [
    { label: 'Restart Pod',      icon: '🔄', actionName: 'restart_pod',        targetNode: node.id },
    { label: 'Isolate Container',icon: '🔒', actionName: 'isolate_container',   targetNode: node.id },
    { label: 'Scale Up',         icon: '📈', actionName: 'scale_up_replicas',   targetNode: node.id },
    { label: 'Block IP',         icon: '🛡', actionName: 'block_ip',            targetNode: node.id },
  ];

  return (
    <div style={{
      background: '#06111F',
      border:     '1px solid #0D2244',
      borderRadius: 12,
      overflow:   'hidden',
      width:      260,
      flexShrink: 0,
    }}>
      {/* Header */}
      <div style={{
        padding:    '12px 14px',
        borderBottom: '1px solid #0D2244',
        display:    'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        background: '#040C1A',
      }}>
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: '#C8D8E8' }}>{node.label}</div>
          <div style={{ fontSize: 10, color: '#4A6A8A', fontFamily: 'JetBrains Mono, monospace', marginTop: 2 }}>
            {LAYER_LABELS[node.layer]}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <GlowBadge severity={node.status === 'healthy' ? 'info' : node.status} label={node.status} />
          <button onClick={onClose} style={{
            background: 'transparent', border: 'none', color: '#4A6A8A',
            cursor: 'pointer', fontSize: 16, lineHeight: 1,
          }}>✕</button>
        </div>
      </div>

      {/* Metrics */}
      <div style={{ padding: '12px 14px' }}>
        <div style={{ marginBottom: 10 }}>
          <MetricBar label="CPU" value={node.cpu} />
        </div>
        <div style={{ marginBottom: 14 }}>
          <MetricBar label="MEM" value={node.mem} />
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 14 }}>
          {[
            { label: 'Namespace', value: node.namespace ?? 'default' },
            { label: 'Restarts',  value: String(node.restarts ?? 0)  },
            { label: 'Node',      value: node.nodeName ?? 'node-01'  },
            { label: 'Layer',     value: node.layer                  },
          ].map(kv => (
            <div key={kv.label} style={{
              background: '#040C1A', borderRadius: 6, padding: '6px 8px',
            }}>
              <div style={{ fontSize: 9, color: '#4A6A8A', textTransform: 'uppercase', marginBottom: 2 }}>
                {kv.label}
              </div>
              <div style={{ fontSize: 11, color: '#00D4FF', fontFamily: 'JetBrains Mono, monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {kv.value}
              </div>
            </div>
          ))}
        </div>

        {/* Quick actions */}
        <div style={{ borderTop: '1px solid #0D2244', paddingTop: 12 }}>
          <div style={{ fontSize: 10, color: '#4A6A8A', marginBottom: 8, textTransform: 'uppercase', letterSpacing: 0.5 }}>
            Ghost Preview Actions
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
            {actions.map(a => (
              <button
                key={a.actionName}
                onClick={() => onGhostPreview(a)}
                style={{
                  background: '#040C1A', border: '1px solid #0D2244',
                  borderRadius: 6, padding: '6px 8px', cursor: 'pointer',
                  fontSize: 11, color: '#C8D8E8', textAlign: 'left',
                  display: 'flex', alignItems: 'center', gap: 5,
                  transition: 'border-color 0.15s',
                }}
                onMouseEnter={e => (e.currentTarget.style.borderColor = '#9B5DE566')}
                onMouseLeave={e => (e.currentTarget.style.borderColor = '#0D2244')}
              >
                <span>{a.icon}</span>
                <span>{a.label}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};

export default NodeCard;

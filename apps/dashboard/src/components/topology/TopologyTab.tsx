import React, { useState, useEffect } from 'react';
import { useClusterStore } from '@/stores/useClusterStore';
import { useTopology } from '@/hooks/useTopology';
import { TopologyMap }     from './TopologyMap';
import { NodeCard }        from './NodeCard';
import { GhostPreviewModal } from '@/components/ghost/GhostPreviewModal';
import { GlowBadge }       from '@/components/shared/GlowBadge';
import type { GhostAction } from '@/types';
import { Zap, Brain, Shield, MessageSquare, Settings } from "lucide-react";

const LAYER_HEALTH = [
  {
    label: "L1 Nervous",
    sub: "eBPF Probes",
    status: "ACTIVE",
    color: "#34D399",
    icon: <Zap size={14} />,
  },
  {
    label: "L2 Cognitive",
    sub: "GNN Active",
    status: "ACTIVE",
    color: "#22D3EE",
    icon: <Brain size={14} />,
  },
  {
    label: "L3 Guardian",
    sub: "OPA Enforced",
    status: "ACTIVE",
    color: "#A78BFA",
    icon: <Shield size={14} />,
  },
  {
    label: "L4 Co-Pilot",
    sub: "LLM Ready",
    status: "STANDBY",
    color: "#FBBF24",
    icon: <MessageSquare size={14} />,
  },
];

export const TopologyTab: React.FC = () => {
  const { nodes, edges, alerts, selectedNode, selectNode, setNodes, setEdges } = useClusterStore();
  const { data: topologyData, loading, error } = useTopology();
  const [ghostAction, setGhostAction] = useState<GhostAction | null>(null);

  // Sync backend topology data to local store
  useEffect(() => {
    if (topologyData && topologyData.nodes.length > 0) {
      setNodes(topologyData.nodes);
      setEdges(topologyData.edges);
      
      // Refresh selectedNode with latest data so side panel stays current
      const currentSel = useClusterStore.getState().selectedNode;
      if (currentSel) {
        const updated = topologyData.nodes.find(n => n.id === currentSel.id);
        if (updated) selectNode(updated);
      }
    }
  }, [topologyData, setNodes, setEdges, selectNode]);

  const criticals = nodes.filter(n => n.status === 'critical').length;
  const warnings  = nodes.filter(n => n.status === 'warning').length;
  const healthy   = nodes.filter(n => n.status === 'healthy').length;

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>

      {/* Stats bar */}
      <div style={{
        padding:     '10px 20px',
        borderBottom:'1px solid #0D2244',
        display:     'flex',
        alignItems:  'center',
        gap:         16,
        flexShrink:  0,
        background:  '#040C1A',
      }}>
        {[
          { label: 'Nodes',    value: nodes.length,  color: '#C8D8E8' },
          { label: 'Critical', value: criticals,      color: '#FF3B5C' },
          { label: 'Warning',  value: warnings,       color: '#FFB800' },
          { label: 'Healthy',  value: healthy,        color: '#00FF9F' },
          { label: 'Causal Edges', value: edges.filter(e => e.causal).length, color: '#FF3B5C' },
        ].map(s => (
          <div key={s.label} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{ fontSize: 18, fontWeight: 700, color: s.color, fontFamily: 'JetBrains Mono, monospace' }}>
              {s.value}
            </span>
            <span style={{ fontSize: 11, color: '#d7ecfaff' }}>{s.label}</span>
          </div>
        ))}
        <div style={{ flex: 1 }} />
        {/* Layer health chips */}
        {LAYER_HEALTH.map(l => (
          <div key={l.label} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <div style={{
              width: 6, height: 6, borderRadius: '50%', background: l.color,
              boxShadow: `0 0 6px ${l.color}`,
            }} />
            <span style={{ fontSize: 10, color: '#b9b9f6ff', fontFamily: 'JetBrains Mono, monospace' }}>
              {l.label}
            </span>
          </div>
        ))}
      </div>

      {/* Main content */}
      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>

        {/* Topology SVG */}
        <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>
          <TopologyMap
            nodes={nodes}
            edges={edges}
            selectedId={selectedNode?.id}
            onSelectNode={selectNode}
          />
          {/* Legend */}
          <div style={{
            position: 'absolute', bottom: 14, left: 14,
            background: '#06111FCC', border: '1px solid #0D2244',
            borderRadius: 8, padding: '8px 12px',
            display: 'flex', gap: 12,
          }}>
            {[
              { color: '#FF3B5C', label: 'Critical / Causal' },
              { color: '#FFB800', label: 'Warning'           },
              { color: '#00FF9F', label: 'Healthy'           },
              { color: '#00D4FF', label: 'Selected'          },
            ].map(l => (
              <div key={l.label} style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                <div style={{ width: 8, height: 8, borderRadius: '50%', background: l.color, boxShadow: `0 0 4px ${l.color}` }} />
                <span style={{ fontSize: 10, color: '#8899AA' }}>{l.label}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Right panel */}
        <div style={{
          width:       280,
          flexShrink:  0,
          borderLeft:  '1px solid #0D2244',
          display:     'flex',
          flexDirection:'column',
          overflow:    'hidden',
        }}>
          {/* Node card or alert list */}
          {selectedNode ? (
            <div style={{ padding: 12, overflowY: 'auto', flex: 1 }}>
              <NodeCard
                node={selectedNode}
                onClose={() => selectNode(null)}
                onGhostPreview={setGhostAction}
              />
            </div>
          ) : (
            <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
              <div style={{ padding: '12px 14px', borderBottom: '1px solid #0D2244', fontSize: 12, fontWeight: 600, color: '#C8D8E8' }}>
                Live Alerts
              </div>
              <div style={{ flex: 1, overflowY: 'auto', padding: '8px 10px' }}>
                {alerts.map((a, i) => (
                  <div key={a.id} style={{
                    padding:     '8px 10px',
                    marginBottom: 6,
                    borderRadius: 7,
                    background:  '#06111F',
                    border:      `1px solid ${a.severity === 'critical' ? '#FF3B5C33' : a.severity === 'warning' ? '#FFB80033' : '#0D2244'}`,
                    animation:   i < 2 ? 'fadeSlideIn 0.3s ease' : undefined,
                  }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
                      <GlowBadge severity={a.severity} label={a.severity} />
                      <span style={{ fontSize: 9, color: '#4A6A8A', fontFamily: 'JetBrains Mono, monospace' }}>{a.time}</span>
                    </div>
                    <div style={{ fontSize: 11, color: '#C8D8E8', lineHeight: 1.4, marginBottom: 2 }}>{a.msg}</div>
                    <div style={{ fontSize: 10, color: '#4A6A8A', fontFamily: 'JetBrains Mono, monospace' }}>{a.node}</div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Ghost Preview Modal */}
      {ghostAction && (
        <GhostPreviewModal action={ghostAction} onClose={() => setGhostAction(null)} />
      )}
    </div>
  );
};

export default TopologyTab;

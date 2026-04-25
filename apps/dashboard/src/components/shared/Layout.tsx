import React, { useState, useEffect } from 'react';
import { useClusterStore }  from '@/stores/useClusterStore';
import { useIncidentStore } from '@/stores/useIncidentStore';
import { ToastContainer }   from './Toast';

// Tab imports
import TopologyTab     from '@/components/topology/TopologyTab';
import IntelligenceTab from '@/components/intelligence/IntelligenceTab';
import GuardianTab     from '@/components/guardian/GuardianTab';
import CopilotTab      from '@/components/copilot/CopilotTab';
import EBPFTab         from '@/components/ebpf/EBPFTab';
import IncidentsTab    from '@/components/incidents/IncidentsTab';
import SettingsTab     from '@/components/shared/SettingsTab';
import NotificationsTab from '@/components/shared/NotificationsTab';

// ─── Nav items ────────────────────────────────────────────────────────────────
type TabId = 'topology' | 'intelligence' | 'guardian' | 'copilot' | 'ebpf' | 'incidents' | 'notifications' | 'settings';

interface NavItem {
  id:    TabId;
  icon:  string;
  label: string;
  badge?: number | string;
}

const NAV_ITEMS: NavItem[] = [
  { id: 'topology',      icon: '⬡',  label: 'Topology'      },
  { id: 'intelligence',  icon: '🧠',  label: 'Intelligence'  },
  { id: 'guardian',      icon: '🛡',  label: 'Guardian'      },
  { id: 'copilot',       icon: '💬',  label: 'Co-Pilot'      },
  { id: 'ebpf',          icon: '⚡',  label: 'eBPF Sensors'  },
  { id: 'incidents',     icon: '🚨',  label: 'Incidents'     },
  { id: 'notifications', icon: '🔔',  label: 'Notifications' },
  { id: 'settings',      icon: '⚙',  label: 'Settings'      },
];

// ─── Styles ───────────────────────────────────────────────────────────────────
const S = {
  root: {
    display:   'flex',
    height:    '100vh',
    width:     '100vw',
    overflow:  'hidden',
    background:'#030810',
  } as React.CSSProperties,
  sidebar: {
    width:          52,
    flexShrink:     0,
    background:     '#050E1F',
    borderRight:    '1px solid #0D2244',
    display:        'flex',
    flexDirection:  'column' as const,
    alignItems:     'center',
    paddingTop:     12,
    paddingBottom:  12,
    gap:            4,
    zIndex:         100,
  } as React.CSSProperties,
  logo: {
    width:        36,
    height:       36,
    borderRadius: 8,
    background:   'linear-gradient(135deg, #00D4FF22, #9B5DE522)',
    border:       '1px solid #00D4FF44',
    display:      'flex',
    alignItems:   'center',
    justifyContent: 'center',
    fontSize:     18,
    marginBottom: 12,
    cursor:       'default',
    flexShrink:   0,
  } as React.CSSProperties,
  main: {
    flex:           1,
    display:        'flex',
    flexDirection:  'column' as const,
    overflow:       'hidden',
  } as React.CSSProperties,
  header: {
    height:         48,
    flexShrink:     0,
    background:     '#050E1F',
    borderBottom:   '1px solid #0D2244',
    display:        'flex',
    alignItems:     'center',
    justifyContent: 'space-between',
    padding:        '0 20px',
  } as React.CSSProperties,
  content: {
    flex:     1,
    overflow: 'hidden',
  } as React.CSSProperties,
} as const;

// ─── Sidebar nav button ────────────────────────────────────────────────────────
function NavBtn({
  item,
  active,
  badge,
  onClick,
}: {
  item:    NavItem;
  active:  boolean;
  badge?:  number | string;
  onClick: () => void;
}) {
  const [hovered, setHovered] = useState(false);

  return (
    <button
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      title={item.label}
      style={{
        position:       'relative',
        width:          40,
        height:         40,
        border:         'none',
        borderRadius:   8,
        cursor:         'pointer',
        display:        'flex',
        alignItems:     'center',
        justifyContent: 'center',
        fontSize:       16,
        background:     active
          ? '#00D4FF15'
          : hovered ? '#0D2244' : 'transparent',
        outline:        active ? '1px solid #00D4FF44' : 'none',
        transition:     'all 0.15s',
        flexShrink:     0,
      }}
    >
      {item.icon}
      {badge !== undefined && Number(badge) > 0 && (
        <span
          style={{
            position:     'absolute',
            top:          2,
            right:        2,
            width:        14,
            height:       14,
            borderRadius: '50%',
            background:   '#FF3B5C',
            color:        '#fff',
            fontSize:     8,
            fontWeight:   700,
            display:      'flex',
            alignItems:   'center',
            justifyContent: 'center',
          }}
        >
          {Number(badge) > 9 ? '9+' : badge}
        </span>
      )}
    </button>
  );
}

// ─── Layer health dots ─────────────────────────────────────────────────────────
const LAYERS = [
  { label: 'L1 eBPF', color: '#00FF9F' },
  { label: 'L2 GNN',  color: '#00D4FF' },
  { label: 'L3 Guard',color: '#9B5DE5' },
  { label: 'L4 LLM',  color: '#FFB800' },
];

// ─── Layout ───────────────────────────────────────────────────────────────────
export const Layout: React.FC = () => {
  const [activeTab, setActiveTab] = useState<TabId>('topology');
  const { clock, setClock, alerts } = useClusterStore();
  const { unreadCount }            = useIncidentStore();

  // Live clock
  useEffect(() => {
    const t = setInterval(() => {
      setClock(new Date().toLocaleTimeString('en-GB', { hour12: false }));
    }, 1000);
    return () => clearInterval(t);
  }, [setClock]);

  // Live node metrics simulation
  const updateNodeMetric = useClusterStore(s => s.updateNodeMetric);
  const nodes = useClusterStore(s => s.nodes);
  useEffect(() => {
    const t = setInterval(() => {
      const node = nodes[Math.floor(Math.random() * nodes.length)];
      if (node) {
        const delta = (Math.random() - 0.5) * 8;
        updateNodeMetric(
          node.id,
          Math.max(5, Math.min(99, node.cpu + delta)),
          Math.max(10, Math.min(95, node.mem + (Math.random() - 0.5) * 4)),
        );
      }
    }, 2000);
    return () => clearInterval(t);
  }, [nodes, updateNodeMetric]);

  const criticalCount = alerts.filter(a => a.severity === 'critical').length;

  const renderTab = () => {
    switch (activeTab) {
      case 'topology':      return <TopologyTab />;
      case 'intelligence':  return <IntelligenceTab />;
      case 'guardian':      return <GuardianTab onOpenGhost={() => {}} />;
      case 'copilot':       return <CopilotTab onOpenGhost={() => {}} />;
      case 'ebpf':          return <EBPFTab />;
      case 'incidents':     return <IncidentsTab />;
      case 'notifications': return <NotificationsTab />;
      case 'settings':      return <SettingsTab />;
      default:              return null;
    }
  };

  const getTabLabel = () =>
    NAV_ITEMS.find(n => n.id === activeTab)?.label ?? '';

  return (
    <div style={S.root}>
      {/* Sidebar */}
      <nav style={S.sidebar}>
        <div style={S.logo}>⬡</div>
        {NAV_ITEMS.map(item => (
          <NavBtn
            key={item.id}
            item={item}
            active={activeTab === item.id}
            badge={
              item.id === 'notifications' ? unreadCount :
              item.id === 'incidents'     ? criticalCount : undefined
            }
            onClick={() => setActiveTab(item.id)}
          />
        ))}
      </nav>

      {/* Main area */}
      <div style={S.main}>
        {/* Header */}
        <header style={S.header}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: '#00D4FF', letterSpacing: 1 }}>
              CCDT
            </span>
            <span style={{ color: '#4A6A8A', fontSize: 12 }}>›</span>
            <span style={{ fontSize: 13, color: '#C8D8E8' }}>{getTabLabel()}</span>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
            {/* Layer health indicators */}
            <div style={{ display: 'flex', gap: 12 }}>
              {LAYERS.map(l => (
                <div key={l.label} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <div style={{
                    width: 6, height: 6, borderRadius: '50%',
                    background: l.color, boxShadow: `0 0 6px ${l.color}`,
                    animation: 'pulse 2s infinite',
                  }} />
                  <span style={{ fontSize: 10, color: '#4A6A8A', fontFamily: 'JetBrains Mono, monospace' }}>
                    {l.label}
                  </span>
                </div>
              ))}
            </div>

            {/* Critical alert badge */}
            {criticalCount > 0 && (
              <div style={{
                background: '#FF3B5C22', border: '1px solid #FF3B5C44',
                borderRadius: 4, padding: '2px 8px',
                fontSize: 11, color: '#FF3B5C', fontFamily: 'JetBrains Mono, monospace',
              }}>
                {criticalCount} CRITICAL
              </div>
            )}

            <span style={{ fontSize: 12, color: '#4A6A8A', fontFamily: 'JetBrains Mono, monospace' }}>
              {clock} UTC
            </span>
          </div>
        </header>

        {/* Tab content */}
        <main style={S.content}>
          {renderTab()}
        </main>
      </div>

      <ToastContainer />

      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50%       { opacity: 0.4; }
        }
        @keyframes spin {
          from { transform: rotate(0deg); }
          to   { transform: rotate(360deg); }
        }
        @keyframes fadeSlideIn {
          from { opacity: 0; transform: translateY(6px); }
          to   { opacity: 1; transform: translateY(0); }
        }
        @keyframes blink {
          0%, 100% { opacity: 1; }
          50%       { opacity: 0; }
        }
      `}</style>
    </div>
  );
};

export default Layout;

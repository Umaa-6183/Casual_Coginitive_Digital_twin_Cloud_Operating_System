import { create } from 'zustand';
import type { ServiceNode, ServiceEdge, Alert, GNNInference } from '@/types';

// ─── Seed topology data (replaces API until backend is connected) ──────────────
const SEED_NODES: ServiceNode[] = [
  { id: 'api-gw',       label: 'API Gateway',      x: 400, y: 60,  status: 'healthy',  layer: 'network',  cpu: 42, mem: 58 },
  { id: 'auth-svc',     label: 'Auth Service',     x: 200, y: 160, status: 'healthy',  layer: 'service',  cpu: 31, mem: 44 },
  { id: 'order-svc',    label: 'Order Service',    x: 400, y: 160, status: 'critical', layer: 'service',  cpu: 94, mem: 87 },
  { id: 'payment-svc',  label: 'Payment Service',  x: 600, y: 160, status: 'warning',  layer: 'service',  cpu: 67, mem: 71 },
  { id: 'inventory-svc',label: 'Inventory Svc',    x: 150, y: 280, status: 'healthy',  layer: 'service',  cpu: 28, mem: 39 },
  { id: 'notify-svc',   label: 'Notify Service',   x: 350, y: 280, status: 'warning',  layer: 'service',  cpu: 73, mem: 62 },
  { id: 'postgres',     label: 'PostgreSQL',       x: 550, y: 280, status: 'critical', layer: 'data',     cpu: 91, mem: 89 },
  { id: 'redis',        label: 'Redis Cache',      x: 200, y: 380, status: 'healthy',  layer: 'data',     cpu: 18, mem: 45 },
  { id: 'kafka',        label: 'Kafka Broker',     x: 420, y: 380, status: 'healthy',  layer: 'system',   cpu: 35, mem: 52 },
  { id: 'monitoring',   label: 'VictoriaMetrics',  x: 620, y: 380, status: 'healthy',  layer: 'system',   cpu: 22, mem: 41 },
];

const SEED_EDGES: ServiceEdge[] = [
  { from: 'api-gw',       to: 'auth-svc',      type: 'grpc',  causal: false },
  { from: 'api-gw',       to: 'order-svc',     type: 'http',  causal: true  },
  { from: 'api-gw',       to: 'payment-svc',   type: 'http',  causal: false },
  { from: 'order-svc',    to: 'postgres',      type: 'tcp',   causal: true  },
  { from: 'order-svc',    to: 'notify-svc',    type: 'kafka', causal: true  },
  { from: 'payment-svc',  to: 'postgres',      type: 'tcp',   causal: false },
  { from: 'inventory-svc',to: 'postgres',      type: 'tcp',   causal: false },
  { from: 'notify-svc',   to: 'kafka',         type: 'kafka', causal: false },
  { from: 'order-svc',    to: 'redis',         type: 'tcp',   causal: false },
  { from: 'monitoring',   to: 'kafka',         type: 'probe', causal: false },
];

const SEED_ALERTS: Alert[] = [
  { id: 1, time: '14:32:01', severity: 'critical', msg: 'privilege escalation detected via CAP_SYS_ADMIN',       node: 'order-svc',   type: 'attack' },
  { id: 2, time: '14:32:03', severity: 'critical', msg: 'PostgreSQL OOM kill – rss=3.8GB limit=4GB',             node: 'postgres',    type: 'fault'  },
  { id: 3, time: '14:32:11', severity: 'warning',  msg: 'TCP retransmit rate 187/s on order→postgres edge',      node: 'order-svc',   type: 'fault'  },
  { id: 4, time: '14:32:18', severity: 'warning',  msg: 'scheduler latency p99=142ms (threshold 50ms)',          node: 'notify-svc',  type: 'fault'  },
  { id: 5, time: '14:32:25', severity: 'info',     msg: 'GNN causal chain identified: privilege→postgres→order', node: 'api-gw',      type: 'attack' },
];

// ─── Store interface ───────────────────────────────────────────────────────────
interface ClusterState {
  nodes:        ServiceNode[];
  edges:        ServiceEdge[];
  alerts:       Alert[];
  inference:    GNNInference | null;
  selectedNode: ServiceNode | null;
  clock:        string;

  // Actions
  setNodes:        (nodes: ServiceNode[]) => void;
  updateNodeMetric:(id: string, cpu: number, mem: number) => void;
  setEdges:        (edges: ServiceEdge[]) => void;
  setAlerts:       (alerts: Alert[]) => void;
  addAlert:        (alert: Alert) => void;
  setInference:    (inf: GNNInference | null) => void;
  selectNode:      (node: ServiceNode | null) => void;
  setClock:        (t: string) => void;
}

export const useClusterStore = create<ClusterState>((set) => ({
  nodes:        SEED_NODES,
  edges:        SEED_EDGES,
  alerts:       SEED_ALERTS,
  inference:    null,
  selectedNode: null,
  clock:        new Date().toLocaleTimeString(),

  setNodes:         (nodes)  => set({ nodes }),
  updateNodeMetric: (id, cpu, mem) =>
    set(s => ({
      nodes: s.nodes.map(n =>
        n.id === id
          ? {
              ...n,
              cpu,
              mem,
              status: cpu > 88 ? 'critical' : cpu > 65 ? 'warning' : 'healthy',
            }
          : n,
      ),
    })),
  setEdges:   (edges)   => set({ edges }),
  setAlerts:  (alerts)  => set({ alerts }),
  addAlert:   (alert)   => set(s => ({ alerts: [alert, ...s.alerts].slice(0, 100) })),
  setInference: (inference) => set({ inference }),
  selectNode: (selectedNode)  => set({ selectedNode }),
  setClock:   (clock)   => set({ clock }),
}));

import { create } from 'zustand';
import type { ServiceNode, ServiceEdge, Alert, GNNInference } from '@/types';

// Initial empty state - will be populated from backend
const SEED_NODES: ServiceNode[] = [];
const SEED_EDGES: ServiceEdge[] = [];
const SEED_ALERTS: Alert[] = [];

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

  setNodes:         (nodes)  => set({
    nodes: nodes.filter(n =>
      typeof n.x === 'number' && isFinite(n.x) &&
      typeof n.y === 'number' && isFinite(n.y) &&
      typeof n.cpu === 'number' && isFinite(n.cpu) &&
      typeof n.mem === 'number' && isFinite(n.mem)
    )
  }),
  updateNodeMetric: (id, cpu, mem) =>
    set(s => ({
      nodes: s.nodes.map(n => {
        if (n.id !== id) return n;
        // Validate metrics are valid numbers
        const validCpu = typeof cpu === 'number' && isFinite(cpu) ? cpu : n.cpu;
        const validMem = typeof mem === 'number' && isFinite(mem) ? mem : n.mem;
        return {
          ...n,
          cpu: validCpu,
          mem: validMem,
          status: validCpu > 88 ? 'critical' : validCpu > 65 ? 'warning' : 'healthy',
        };
      }),
    })),
  setEdges:   (edges)   => set({ edges }),
  setAlerts:  (alerts)  => set({ alerts }),
  addAlert:   (alert)   => set(s => ({ alerts: [alert, ...s.alerts].slice(0, 100) })),
  setInference: (inference) => set({ inference }),
  selectNode: (selectedNode)  => set({ selectedNode }),
  setClock:   (clock)   => set({ clock }),
}));

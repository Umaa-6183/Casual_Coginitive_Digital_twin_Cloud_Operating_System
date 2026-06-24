import { useEffect } from 'react';
import { useClusterStore } from '@/stores/useClusterStore';
import { useGNN } from './useGNN';

/**
 * Cross-layer synchronization hook
 *
 * Ensures that Topology, Intelligence, Guardian, Incidents, and Ghost Preview
 * all stay synchronized to the same backend scenario state.
 *
 * When a scenario changes in the backend:
 * - Topology updates node health and causal edges
 * - Intelligence updates classifications and root cause
 * - Guardian updates recommended actions
 * - Incidents reflect the current state
 * - Ghost Preview simulates based on current context
 */
export function useScenarioSync() {
  const { inference } = useGNN();
  const { nodes, setNodes, edges, setEdges } = useClusterStore();

  // Sync topology node status with GNN inference classifications
  useEffect(() => {
    if (!inference || nodes.length === 0) return;

    const updatedNodes = nodes.map(node => {
      const classification = inference.nodeClassifications[node.id];
      if (!classification) return node;

      // Determine status based on GNN classification
      const isAttack = classification.attack > 0.7;
      const isFault = classification.fault > 0.7;
      const isHealthy = classification.healthy > 0.7;

      let status: 'critical' | 'warning' | 'healthy' = 'healthy';
      if (isAttack || isFault) {
        status = 'critical';
      } else if (classification.attack > 0.3 || classification.fault > 0.3) {
        status = 'warning';
      }

      return { ...node, status };
    });

    // Only update if status changed
    const hasChanges = updatedNodes.some((node, i) => node.status !== nodes[i].status);
    if (hasChanges) {
      setNodes(updatedNodes);
    }
  }, [inference, nodes, setNodes]);

  // Sync causal edges with GNN causal chain
  useEffect(() => {
    if (!inference || edges.length === 0) return;

    const causalNodeIds = new Set(inference.causalChain.map(item => item.node));

    const updatedEdges = edges.map(edge => {
      // Mark edge as causal if both nodes are in the causal chain
      const isCausal = causalNodeIds.has(edge.from) && causalNodeIds.has(edge.to);
      return { ...edge, causal: isCausal };
    });

    // Only update if causal flags changed
    const hasChanges = updatedEdges.some((edge, i) => edge.causal !== edges[i].causal);
    if (hasChanges) {
      setEdges(updatedEdges);
    }
  }, [inference, edges, setEdges]);
}

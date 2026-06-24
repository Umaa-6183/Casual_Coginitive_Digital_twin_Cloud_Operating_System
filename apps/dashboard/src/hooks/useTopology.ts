import { useEffect, useState } from 'react';
import type { ServiceNode, ServiceEdge } from '@/types';
import { fetchTopology } from '@/api/client';

interface TopologyData {
  nodes: ServiceNode[];
  edges: ServiceEdge[];
}

// Fallback topology data when backend is offline
const FALLBACK_NODES: ServiceNode[] = [
  { id: 'api-gw',       label: 'API Gateway',      x: 400, y: 60,  status: 'healthy',  layer: 'network',  cpu: 42, mem: 58, namespace: 'default', nodeName: 'node-01', restarts: 0 },
  { id: 'auth-svc',     label: 'Auth Service',     x: 200, y: 160, status: 'healthy',  layer: 'service',  cpu: 31, mem: 44, namespace: 'default', nodeName: 'node-02', restarts: 0 },
  { id: 'order-svc',    label: 'Order Service',    x: 400, y: 160, status: 'critical', layer: 'service',  cpu: 94, mem: 87, namespace: 'default', nodeName: 'node-01', restarts: 3 },
  { id: 'payment-svc',  label: 'Payment Service',  x: 600, y: 160, status: 'warning',  layer: 'service',  cpu: 67, mem: 71, namespace: 'default', nodeName: 'node-03', restarts: 1 },
  { id: 'inventory-svc',label: 'Inventory Svc',    x: 150, y: 280, status: 'healthy',  layer: 'service',  cpu: 28, mem: 39, namespace: 'default', nodeName: 'node-02', restarts: 0 },
  { id: 'notify-svc',   label: 'Notify Service',   x: 350, y: 280, status: 'warning',  layer: 'service',  cpu: 73, mem: 62, namespace: 'default', nodeName: 'node-01', restarts: 1 },
  { id: 'postgres',     label: 'PostgreSQL',       x: 550, y: 280, status: 'critical', layer: 'data',     cpu: 91, mem: 89, namespace: 'default', nodeName: 'node-02', restarts: 2 },
  { id: 'redis',        label: 'Redis Cache',      x: 200, y: 380, status: 'healthy',  layer: 'data',     cpu: 18, mem: 45, namespace: 'default', nodeName: 'node-03', restarts: 0 },
  { id: 'kafka',        label: 'Kafka Broker',     x: 420, y: 380, status: 'healthy',  layer: 'system',   cpu: 35, mem: 52, namespace: 'default', nodeName: 'node-02', restarts: 0 },
  { id: 'monitoring',   label: 'VictoriaMetrics',  x: 620, y: 380, status: 'healthy',  layer: 'system',   cpu: 22, mem: 41, namespace: 'monitoring', nodeName: 'node-03', restarts: 0 },
];

const FALLBACK_EDGES: ServiceEdge[] = [
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

export function mutateFallbackData(): TopologyData {
  const tick = Date.now() / 1000;
  const cycle = (tick / 60) % 1.0;
  
  const nodes = FALLBACK_NODES.map(node => {
    let baseCpu = 30 + (Math.random() - 0.5) * 10;
    let baseMem = 40 + (Math.random() - 0.5) * 10;
    
    if (node.id === 'order-svc' || node.id === 'postgres') {
      if (cycle >= 0.4 && cycle < 0.7) {
        baseCpu = 90 + Math.random() * 5;
        baseMem = 90 + Math.random() * 5;
      } else if (cycle >= 0.7) {
        const progress = (cycle - 0.7) / 0.3;
        baseCpu = 90 - (60 * progress);
        baseMem = 90 - (50 * progress);
      }
    } else if (node.id === 'payment-svc') {
      if (cycle >= 0.4 && cycle < 0.7) {
        baseCpu = 70 + Math.random() * 5;
      } else if (cycle >= 0.7) {
        const progress = (cycle - 0.7) / 0.3;
        baseCpu = 70 - (40 * progress);
      }
    }
    
    const cpu = Math.max(5, Math.min(95, baseCpu));
    const mem = Math.max(10, Math.min(95, baseMem));
    const status = cpu > 85 || mem > 90 ? 'critical' : cpu > 65 || mem > 75 ? 'warning' : 'healthy';
    
    return { ...node, cpu, mem, status: status as 'critical' | 'warning' | 'healthy' };
  });

  const edges = FALLBACK_EDGES.map(edge => {
    const srcNode = nodes.find(n => n.id === edge.from);
    const dstNode = nodes.find(n => n.id === edge.to);
    const srcStatus = srcNode?.status || 'healthy';
    const dstStatus = dstNode?.status || 'healthy';
    const causal = srcStatus === 'critical' || dstStatus === 'critical';
    return { ...edge, causal };
  });

  return { nodes, edges };
}

export function useTopology() {
  const [data, setData] = useState<TopologyData>({
    nodes: FALLBACK_NODES,
    edges: FALLBACK_EDGES,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;

    async function loadTopology() {
      try {
        setError(null);

        const topologyData = await fetchTopology();

        if (mounted && topologyData && topologyData.nodes && topologyData.nodes.length > 0) {
          // Validate that nodes have valid coordinates (not NaN or undefined)
          const validNodes = topologyData.nodes.filter((n: any) =>
            typeof n.x === 'number' && typeof n.y === 'number' &&
            isFinite(n.x) && isFinite(n.y) &&
            typeof n.cpu === 'number' && isFinite(n.cpu) &&
            typeof n.mem === 'number' && isFinite(n.mem)
          );

          if (validNodes.length === 0) {
            console.warn('Backend topology data has invalid coordinates, using fallback');
            return; // Keep using fallback data
          }

          console.log('Topology data updated:', {
            nodes: validNodes.length,
            edges: topologyData.edges.length,
            critical: validNodes.filter((n: any) => n.status === 'critical').length,
            warning: validNodes.filter((n: any) => n.status === 'warning').length,
          });

          setData({
            nodes: validNodes as ServiceNode[],
            edges: topologyData.edges as ServiceEdge[],
          });
        }
      } catch (err) {
        if (mounted) {
          console.warn('Backend topology unavailable, using fallback data:', err);
          setData(mutateFallbackData());
          setError(null);
        }
      } finally {
        if (mounted) {
          setLoading(false);
        }
      }
    }

    // Load topology immediately and poll every 3s (faster refresh for topology)
    loadTopology();
    const interval = setInterval(loadTopology, 3000);

    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  return { data, loading, error };
}

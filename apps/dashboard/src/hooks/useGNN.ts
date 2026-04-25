import { useEffect, useState } from 'react';
import type { GNNInference } from '@/types';

// Simulated GNN inference data — replace with fetchInference() when backend is live
const MOCK_INFERENCE: GNNInference = {
  nodeClassifications: {
    'api-gw':        { healthy: 0.91, fault: 0.06, attack: 0.03 },
    'auth-svc':      { healthy: 0.88, fault: 0.09, attack: 0.03 },
    'order-svc':     { healthy: 0.03, fault: 0.11, attack: 0.86 },
    'payment-svc':   { healthy: 0.62, fault: 0.28, attack: 0.10 },
    'inventory-svc': { healthy: 0.93, fault: 0.05, attack: 0.02 },
    'notify-svc':    { healthy: 0.31, fault: 0.64, attack: 0.05 },
    'postgres':      { healthy: 0.04, fault: 0.94, attack: 0.02 },
    'redis':         { healthy: 0.81, fault: 0.17, attack: 0.02 },
    'kafka':         { healthy: 0.94, fault: 0.05, attack: 0.01 },
    'monitoring':    { healthy: 0.97, fault: 0.02, attack: 0.01 },
  },
  graphClassification: { healthy: 0.04, fault: 0.14, attack: 0.82 },
  rootCauseNode:       'order-svc',
  rootCauseConfidence: 0.942,
  incidentType:        'attack',
  blastRadius:         ['order-svc', 'postgres', 'notify-svc'],
  causalChain: [
    { node: 'order-svc',   causalScore: 0.942, status: 'critical' },
    { node: 'postgres',    causalScore: 0.871, status: 'critical' },
    { node: 'notify-svc',  causalScore: 0.634, status: 'warning'  },
    { node: 'payment-svc', causalScore: 0.281, status: 'warning'  },
    { node: 'api-gw',      causalScore: 0.063, status: 'healthy'  },
  ],
  inferenceMs: 8.4,
};

export function useGNN() {
  const [inference, setInference] = useState<GNNInference>(MOCK_INFERENCE);
  const [loading,   setLoading]   = useState(false);

  // In production: call fetchInference() every 5s
  useEffect(() => {
    // Simulate slight variation in scores
    const interval = setInterval(() => {
      setInference(prev => ({
        ...prev,
        inferenceMs:         +(7 + Math.random() * 5).toFixed(1),
        rootCauseConfidence: +(0.91 + Math.random() * 0.05).toFixed(3),
      }));
    }, 5000);
    return () => clearInterval(interval);
  }, []);

  return { inference, loading };
}

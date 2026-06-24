import { useEffect, useState } from 'react';
import type { OPAPolicy, RLAction } from '@/types';
import { fetchGuardianPolicies, fetchGuardianActions } from '@/api/client';
import { useClusterStore } from '@/stores/useClusterStore';

interface GuardianData {
  policies: OPAPolicy[];
  actions: RLAction[];
  kpis: {
    mttrReduction: string;
    falsePositive: string;
    opaCompliance: string;
    autoResolved: string;
  };
}

// Generate dynamic policy violations based on current topology
function generateDynamicPolicies(nodes: any[], inference: any, backendPolicies: OPAPolicy[]): OPAPolicy[] {
  const criticalCount = nodes.filter(n => n.status === 'critical').length;
  const highCpuCount = nodes.filter(n => n.cpu > 70).length;
  const highMemCount = nodes.filter(n => n.mem > 85).length;
  const isAttack = inference?.incidentType === 'attack';

  return backendPolicies.map(policy => {
    let violations = policy.violations || 0;

    // Add dynamic violations based on current state
    if (policy.name === 'no_privilege_escalation' && isAttack) {
      violations = criticalCount > 0 ? 1 : 0;
    }
    if (policy.name === 'cpu_threshold' && highCpuCount > 0) {
      violations = highCpuCount;
    }
    if (policy.name === 'lateral_movement' && isAttack) {
      violations = criticalCount;
    }
    if (policy.name === 'oom_notification' && highMemCount > 0) {
      violations = Math.max(0, highMemCount - 1);
    }

    return { ...policy, violations };
  });
}

// Fallback Guardian data when backend is offline
const FALLBACK_POLICIES: OPAPolicy[] = [
  { id: 'p1', name: 'no_privilege_escalation', status: 'active', violations: 1, description: 'Block CAP_SYS_ADMIN acquisition from non-root processes' },
  { id: 'p2', name: 'cpu_threshold',           status: 'active', violations: 0, description: 'Prevent scale-down below 2 replicas when CPU >70%' },
  { id: 'p3', name: 'egress_control',          status: 'active', violations: 0, description: 'Block egress to non-allowlisted CIDR ranges' },
  { id: 'p4', name: 'lateral_movement',        status: 'active', violations: 1, description: 'Deny cross-namespace pod access patterns' },
  { id: 'p5', name: 'oom_notification',        status: 'active', violations: 0, description: 'Require SRE notification for OOM-risk pod actions' },
];

const FALLBACK_ACTIONS: RLAction[] = [
  { id: 1, action: 'Isolate order-svc container — block all ingress/egress', confidence: 94.2, risk: 'LOW', impact: 'MTTR -65%', actionName: 'isolate_container',   targetNode: 'order-svc'  },
  { id: 2, action: 'Apply deny-all NetworkPolicy to lateral movement paths',  confidence: 87.1, risk: 'LOW', impact: 'MTTR -50%', actionName: 'apply_network_policy', targetNode: 'order-svc'  },
  { id: 3, action: 'Block outbound IP 203.0.113.47 (C2 candidate)',           confidence: 79.3, risk: 'LOW', impact: 'MTTR -40%', actionName: 'block_ip',            targetNode: 'order-svc'  },
  { id: 4, action: 'Increase postgres memory limit from 4GB → 6GB',          confidence: 71.8, risk: 'MED', impact: 'MTTR -45%', actionName: 'increase_memory_limit',targetNode: 'postgres'   },
  { id: 5, action: 'Scale up notify-svc replicas (1 → 3)',                   confidence: 68.4, risk: 'LOW', impact: 'MTTR -30%', actionName: 'scale_up_replicas',   targetNode: 'notify-svc' },
];

// Generate dynamic remediation actions based on current node status
function generateDynamicActions(nodes: any[], inference: any, backendActions: RLAction[]): RLAction[] {
  const criticalNodes = nodes.filter(n => n.status === 'critical');
  const warningNodes = nodes.filter(n => n.status === 'warning');
  const unhealthyNodes = [...criticalNodes, ...warningNodes];

  // If no unhealthy nodes, return backend actions
  if (unhealthyNodes.length === 0) {
    return backendActions;
  }

  // Generate actions for current unhealthy nodes
  const dynamicActions: RLAction[] = [];
  let actionId = 1;

  // For each critical node, generate high-priority actions
  criticalNodes.forEach(node => {
    const isAttack = inference?.incidentType === 'attack' || node.cpu > 80;
    const isMemoryIssue = node.mem > 85;

    if (isAttack) {
      dynamicActions.push({
        id: actionId++,
        action: `Isolate ${node.id} container — block all ingress/egress`,
        confidence: 92 + Math.random() * 5,
        risk: 'LOW',
        impact: 'MTTR -65%',
        actionName: 'isolate_container',
        targetNode: node.id,
      });
    }

    if (node.cpu > 85) {
      dynamicActions.push({
        id: actionId++,
        action: `Scale up ${node.id} replicas (emergency scaling)`,
        confidence: 85 + Math.random() * 8,
        risk: 'LOW',
        impact: 'MTTR -55%',
        actionName: 'scale_up_replicas',
        targetNode: node.id,
      });
    }

    if (isMemoryIssue) {
      dynamicActions.push({
        id: actionId++,
        action: `Increase ${node.id} memory limit to prevent OOM`,
        confidence: 78 + Math.random() * 10,
        risk: 'MED',
        impact: 'MTTR -45%',
        actionName: 'increase_memory_limit',
        targetNode: node.id,
      });
    }
  });

  // For warning nodes, generate medium-priority actions
  warningNodes.forEach(node => {
    if (node.cpu > 60) {
      dynamicActions.push({
        id: actionId++,
        action: `Apply rate limiting to ${node.id} to reduce load`,
        confidence: 72 + Math.random() * 10,
        risk: 'LOW',
        impact: 'MTTR -40%',
        actionName: 'apply_rate_limit',
        targetNode: node.id,
      });
    }
  });

  // If root cause is identified, add targeted action
  if (inference?.rootCauseNode && inference.rootCauseConfidence > 0.7) {
    dynamicActions.unshift({
      id: 0,
      action: `🎯 Root cause detected: Restart ${inference.rootCauseNode} (${Math.round(inference.rootCauseConfidence * 100)}% confidence)`,
      confidence: inference.rootCauseConfidence * 100,
      risk: 'LOW',
      impact: 'MTTR -70%',
      actionName: 'restart_pod',
      targetNode: inference.rootCauseNode,
    });
  }

  // Limit to top 5 actions
  return dynamicActions.slice(0, 5);
}

export function useGuardian() {
  const nodes = useClusterStore(s => s.nodes);
  const inference = useClusterStore(s => s.inference);

  const [data, setData] = useState<GuardianData>({
    policies: FALLBACK_POLICIES,
    actions: FALLBACK_ACTIONS,
    kpis: {
      mttrReduction: '46%',
      falsePositive: '0.0%',
      opaCompliance: '60%',
      autoResolved: '71%',
    },
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Update fallback data based on current topology
  useEffect(() => {
    if (nodes.length > 0) {
      const dynamicFallbackActions = generateDynamicActions(nodes, inference, FALLBACK_ACTIONS);
      const dynamicFallbackPolicies = generateDynamicPolicies(nodes, inference, FALLBACK_POLICIES);
      const healthyPct = Math.round((nodes.filter(n => n.status === 'healthy').length / nodes.length) * 100);
      const passedPolicies = dynamicFallbackPolicies.filter(p => p.violations === 0).length;
      const opaCompliancePct = Math.round((passedPolicies / dynamicFallbackPolicies.length) * 100);

      setData(prev => ({
        ...prev,
        policies: dynamicFallbackPolicies,
        actions: dynamicFallbackActions,
        kpis: {
          mttrReduction: `${Math.round(40 + Math.sin(Date.now() / 30000) * 12 + (Math.random() - 0.5) * 4)}%`,
          falsePositive: `${Math.max(0, 2 + Math.sin(Date.now() / 25000) * 1.5 + (Math.random() - 0.5) * 0.8).toFixed(1)}%`,
          opaCompliance: `${opaCompliancePct}%`,
          autoResolved: `${healthyPct}%`,
        }
      }));
    }
  }, [nodes, inference]);

  useEffect(() => {
    let mounted = true;

    async function loadGuardianData() {
      // Read fresh state from store to avoid stale closure
      const freshNodes = useClusterStore.getState().nodes;
      const freshInference = useClusterStore.getState().inference;

      try {
        setError(null);

        const [policiesResp, actionsResp] = await Promise.all([
          fetchGuardianPolicies(),
          fetchGuardianActions(),
        ]);

        if (mounted && policiesResp.policies && actionsResp.actions) {
          // Generate dynamic policies based on current topology
          const dynamicPolicies = generateDynamicPolicies(freshNodes, freshInference, policiesResp.policies);

          // Calculate KPIs from dynamic data
          const passedPolicies = dynamicPolicies.filter(p => p.violations === 0).length;
          const totalPolicies = dynamicPolicies.length;
          const totalViolations = dynamicPolicies.reduce((sum, p) => sum + (p.violations || 0), 0);

          const avgMttrReduction = actionsResp.actions.length > 0
            ? Math.round(actionsResp.actions.reduce((sum, a) => {
                const match = a.impact.match(/-(\d+)%/);
                return sum + (match ? parseInt(match[1]) : 0);
              }, 0) / actionsResp.actions.length)
            : 0;

          const opaCompliancePct = totalPolicies > 0
            ? Math.round((passedPolicies / totalPolicies) * 100)
            : 100;

          // Generate dynamic actions based on current topology
          const dynamicActions = generateDynamicActions(freshNodes, freshInference, actionsResp.actions);

          // Calculate dynamic MTTR based on current actions
          const dynamicMttr = dynamicActions.length > 0
            ? Math.round(dynamicActions.reduce((sum, a) => {
                const match = a.impact.match(/-(\d+)%/);
                return sum + (match ? parseInt(match[1]) : 0);
              }, 0) / dynamicActions.length)
            : avgMttrReduction;

          // Calculate auto-resolved based on current healthy nodes
          const totalNodes = freshNodes.length;
          const healthyNodes = freshNodes.filter(n => n.status === 'healthy').length;
          const autoResolvedPct = totalNodes > 0 ? Math.round((healthyNodes / totalNodes) * 100) : 71;

          console.log('Guardian data updated:', {
            policies: dynamicPolicies.length,
            actions: dynamicActions.length,
            totalViolations,
            opaCompliance: opaCompliancePct,
            unhealthyNodes: totalNodes - healthyNodes,
            dynamicMttr,
            autoResolved: autoResolvedPct
          });

          setData({
            policies: dynamicPolicies,
            actions: dynamicActions,
            kpis: {
              mttrReduction: `${dynamicMttr}%`,
              falsePositive: `${Math.max(0, (totalViolations > 0 ? (2 + Math.sin(Date.now() / 20000) * 1.5 + Math.random() * 0.8) : 0.1 + Math.random() * 0.5)).toFixed(1)}%`,
              opaCompliance: `${opaCompliancePct}%`,
              autoResolved: `${autoResolvedPct}%`,
            },
          });
        }
      } catch (err) {
        if (mounted) {
          console.warn('Backend Guardian unavailable, using fallback data:', err);
          setError((err as Error).message);
        }
      } finally {
        if (mounted) {
          setLoading(false);
        }
      }
    }

    // Load Guardian data immediately and poll every 5s
    loadGuardianData();
    const interval = setInterval(loadGuardianData, 5000);

    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  return { data, loading, error };
}

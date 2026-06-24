import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { Incident, Notification } from '@/types';

const SEED_INCIDENTS: Incident[] = [
  {
    id: "INC-2847",
    title: "Privilege Escalation → PostgreSQL OOM Cascade",
    severity: "critical",
    status: "active",
    type: "attack",
    opened: "14:32:01",
    elapsed: "00:04:32",
    mttrTarget: "00:15:00",
    node: "order-svc",
    rootCause:
      "Attacker exploited container misconfiguration to gain CAP_SYS_ADMIN, " +
      "enabling direct memory manipulation that caused PostgreSQL OOM kill, " +
      "cascading to order-svc TCP retransmit storm.",
    affected: ["order-svc", "postgres", "payment-svc", "notify-svc"],
    confidence: 94.2,
    autoAction:
      "Container isolation applied. Network policy blocking lateral movement.",
    timeline: [
      {
        time: "14:32:01",
        event: "CAP_SYS_ADMIN acquired via setcap on order-svc (eBPF detected)",
        icon: "critical",
      },
      {
        time: "14:32:03",
        event: "PostgreSQL OOM kill — rss exceeded 4GB limit",
        icon: "critical",
      },
      {
        time: "14:32:05",
        event:
          "GNN causal chain confirmed: 94.2% confidence attack classification",
        icon: "brain",
      },
      {
        time: "14:32:08",
        event: "Guardian RL proposed: isolate_container + block_ip",
        icon: "shield",
      },
      {
        time: "14:32:09",
        event: "Ghost Preview simulated — risk 12/100, OPA PASS",
        icon: "ghost",
      },
      {
        time: "14:32:11",
        event: "OPA policies validated (5/5 passed)",
        icon: "success",
      },
      {
        time: "14:32:12",
        event: "Container isolated. NetworkPolicy applied.",
        icon: "zap",
      },
      {
        time: "14:32:25",
        event: "TCP retransmit rate declining (187→34/s)",
        icon: "trend_down",
      },
    ],
  },
  {
    id: "INC-2846",
    title: "Notify Service Scheduler Latency Spike",
    severity: "warning",
    status: "investigating",
    type: "fault",
    opened: "14:18:44",
    elapsed: "00:17:49",
    mttrTarget: "00:30:00",
    node: "notify-svc",
    rootCause:
      "Scheduler latency p99 spike to 142ms caused by CPU contention with order-svc " +
      "OOM pressure on same physical node.",
    affected: ["notify-svc", "kafka"],
    confidence: 87.1,
    autoAction: "Scale-up triggered. Additional replica scheduled.",
    timeline: [
      {
        time: "14:18:44",
        event: "sched_latency p99 exceeded 50ms threshold (eBPF)",
        icon: "warning",
      },
      {
        time: "14:19:01",
        event: "GNN root cause: CPU contention from order-svc node",
        icon: "brain",
      },
      {
        time: "14:19:15",
        event: "Guardian proposed: scale_up_replicas notify-svc",
        icon: "shield",
      },
      {
        time: "14:19:20",
        event: "Ghost Preview: risk 10/100, MTTR -30%",
        icon: "ghost",
      },
      {
        time: "14:19:22",
        event: "Scale-up approved + executing",
        icon: "zap",
      },
    ],
  },
  {
    id: "INC-2845",
    title: "Redis Cache Eviction Storm",
    severity: "warning",
    status: "auto-resolved",
    type: "fault",
    opened: "13:55:12",
    elapsed: "00:08:20",
    mttrTarget: "00:30:00",
    node: "redis",
    rootCause:
      "Memory pressure from order-svc query flood caused Redis maxmemory eviction storm.",
    affected: ["redis", "order-svc"],
    confidence: 91.3,
    autoAction: "Memory limit increased. Eviction policy tuned to allkeys-lru.",
    timeline: [
      {
        time: "13:55:12",
        event: "Redis eviction rate > 10k/s detected",
        icon: "warning",
      },
      {
        time: "13:55:30",
        event: "GNN: query flood from order-svc identified",
        icon: "brain",
      },
      {
        time: "13:56:00",
        event: "Memory limit +50% applied autonomously",
        icon: "zap",
      },
      {
        time: "14:03:32",
        event: "Eviction rate normalised. Auto-resolved.",
        icon: "success",
      },
    ],
  },
  {
    id: "INC-2844",
    title: "API Gateway Elevated Error Rate",
    severity: "info",
    status: "resolved",
    type: "fault",
    opened: "12:10:05",
    elapsed: "00:06:44",
    mttrTarget: "00:30:00",
    node: "api-gw",
    rootCause: "Upstream order-svc timeout caused 503 cascade on API Gateway.",
    affected: ["api-gw"],
    confidence: 96.0,
    autoAction: "Circuit breaker enabled. Retry budget enforced.",
    timeline: [
      {
        time: "12:10:05",
        event: "Error rate 8.4% (threshold 2%)",
        icon: "warning",
      },
      {
        time: "12:10:20",
        event: "GNN: upstream timeout from order-svc",
        icon: "brain",
      },
      {
        time: "12:12:00",
        event: "Circuit breaker applied",
        icon: "zap",
      },
      {
        time: "12:16:49",
        event: "Error rate < 0.5%. Resolved.",
        icon: "success",
      },
    ],
  },
];

const SEED_NOTIFICATIONS: Notification[] = [
  { id: 1, type: 'alert',    msg: 'CRITICAL: CAP_SYS_ADMIN privilege escalation on order-svc',  time: '14:32:01', read: false, source: 'eBPF'    },
  { id: 2, type: 'alert',    msg: 'CRITICAL: PostgreSQL OOM kill – memory limit exceeded',        time: '14:32:03', read: false, source: 'eBPF'    },
  { id: 3, type: 'action',   msg: 'Guardian: container isolation applied to order-svc',           time: '14:32:12', read: false, source: 'Guardian'},
  { id: 4, type: 'policy',   msg: 'OPA: all 5 policies passed for isolate_container action',      time: '14:32:11', read: false, source: 'OPA'     },
  { id: 5, type: 'resolved', msg: 'INC-2845 Redis Cache Eviction auto-resolved',                  time: '14:03:32', read: true,  source: 'Guardian'},
  { id: 6, type: 'info',     msg: 'GNN inference: causal chain identified with 94.2% confidence', time: '14:32:05', read: true,  source: 'GNN'     },
  { id: 7, type: 'action',   msg: 'Ghost Preview simulation complete — risk 12/100',              time: '14:32:09', read: true,  source: 'Ghost'   },
  { id: 8, type: 'alert',    msg: 'WARNING: notify-svc scheduler latency p99 = 142ms',           time: '14:18:44', read: true,  source: 'eBPF'    },
];

interface IncidentState {
  incidents:     Incident[];
  notifications: Notification[];
  selected:      Incident | null;
  statusFilter:  string;
  unreadCount:   number;

  setSelected:    (inc: Incident | null) => void;
  setFilter:      (f: string) => void;
  setIncidents:   (incs: Incident[]) => void;
  addIncident:    (inc: Incident) => void;
  updateIncident: (id: string, updates: Partial<Incident>) => void;
  markAllRead:    () => void;
  markRead:       (id: number) => void;
}

export const useIncidentStore = create<IncidentState>()(
  persist(
    (set, get) => ({
      incidents:     SEED_INCIDENTS,
      notifications: SEED_NOTIFICATIONS,
      selected:      null,
      statusFilter:  'all',
      unreadCount:   SEED_NOTIFICATIONS.filter(n => !n.read).length,

      setSelected: (selected) => set({ selected }),
      setFilter:   (statusFilter) => set({ statusFilter }),
      setIncidents: (incidents) => set({ incidents }),
      addIncident: (inc) =>
        set(s => ({ incidents: [inc, ...s.incidents] })),
      updateIncident: (id, updates) =>
        set(s => ({
          incidents: s.incidents.map(inc =>
            inc.id === id ? { ...inc, ...updates } : inc
          ),
        })),
      markAllRead: () =>
        set(s => ({
          notifications: s.notifications.map(n => ({ ...n, read: true })),
          unreadCount:   0,
        })),
      markRead: (id) =>
        set(s => {
          const updated = s.notifications.map(n =>
            n.id === id ? { ...n, read: true } : n,
          );
          return { notifications: updated, unreadCount: updated.filter(n => !n.read).length };
        }),
    }),
    {
      name: 'ccdt-incident-storage',
      partialize: (state) => ({
        incidents: state.incidents,
        notifications: state.notifications,
      }),
    }
  )
);

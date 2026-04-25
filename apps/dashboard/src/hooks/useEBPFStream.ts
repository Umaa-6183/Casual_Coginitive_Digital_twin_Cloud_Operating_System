import { useEffect, useRef, useState, useCallback } from 'react';
import type { EBPFEvent, EBPFEventType, Severity } from '@/types';

// ─── Seed generators ───────────────────────────────────────────────────────────
const PODS     = ['order-svc', 'postgres', 'payment-svc', 'notify-svc', 'redis', 'api-gw'];
const NODES    = ['node-01', 'node-02', 'node-03'];
const TYPES: EBPFEventType[] = ['syscall', 'oom', 'tcp', 'sched', 'file', 'capability', 'probe'];

const TEMPLATES: Record<EBPFEventType, { detail: () => string; severity: Severity }> = {
  syscall:    { detail: () => `execve("/usr/bin/curl") uid=${Math.floor(Math.random()*1000)}`, severity: 'warning'  },
  oom:        { detail: () => `OOM kill rss=${(3.2 + Math.random()).toFixed(1)}GB limit=4GB`,  severity: 'critical' },
  tcp:        { detail: () => `retransmit ${Math.floor(Math.random()*300)}/s rtt=${Math.floor(Math.random()*200)}µs`, severity: 'warning' },
  sched:      { detail: () => `latency_p99=${Math.floor(50+Math.random()*150)}ms cpu=${Math.floor(Math.random()*4)}`, severity: 'warning' },
  file:       { detail: () => `/etc/shadow read-attempt uid=${Math.floor(Math.random()*500)}`, severity: 'critical' },
  capability: { detail: () => `CAP_SYS_ADMIN granted pid=${Math.floor(1000+Math.random()*9000)}`, severity: 'critical' },
  probe:      { detail: () => `probe_attach sched_switch cpu_overhead=0.12%`,                  severity: 'info'     },
};

function makeEvent(id: number): EBPFEvent {
  const type = TYPES[Math.floor(Math.random() * TYPES.length)];
  const tpl  = TEMPLATES[type];
  const now  = new Date();
  return {
    id,
    ts:       `${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}:${String(now.getSeconds()).padStart(2,'0')}.${String(now.getMilliseconds()).padStart(3,'0')}`,
    type,
    pod:      PODS[Math.floor(Math.random() * PODS.length)],
    node:     NODES[Math.floor(Math.random() * NODES.length)],
    detail:   tpl.detail(),
    severity: tpl.severity,
  };
}

// ─── Hook ─────────────────────────────────────────────────────────────────────
const RING_SIZE = 200;

export function useEBPFStream() {
  const [events,   setEvents]   = useState<EBPFEvent[]>(() =>
    Array.from({ length: 30 }, (_, i) => makeEvent(i + 1)),
  );
  const [paused,   setPaused]   = useState(false);
  const [filter,   setFilter]   = useState<EBPFEventType | 'all'>('all');
  const idRef  = useRef(31);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const start = useCallback(() => {
    timerRef.current = setInterval(() => {
      const evt = makeEvent(idRef.current++);
      setEvents(prev => {
        const next = [evt, ...prev];
        return next.length > RING_SIZE ? next.slice(0, RING_SIZE) : next;
      });
    }, 1400);
  }, []);

  const stop = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => {
    if (!paused) start();
    else stop();
    return stop;
  }, [paused, start, stop]);

  const filtered = filter === 'all' ? events : events.filter(e => e.type === filter);
  const stats = {
    total:    events.length,
    critical: events.filter(e => e.severity === 'critical').length,
    warning:  events.filter(e => e.severity === 'warning').length,
    rate:     Math.round(1000 / 1400),
  };

  return { events: filtered, paused, setPaused, filter, setFilter, stats };
}

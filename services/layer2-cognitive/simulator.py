#!/usr/bin/env python3
"""
CCDT — Enhanced macOS Cluster Simulator (Option D)
════════════════════════════════════════════════════════════════════════════════
Now with two major enhancements:

  Enhancement 1 — Real fault injection via Docker API:
    Instead of just publishing fake metric numbers, the simulator also
    calls the Docker API to actually stress demo-postgres, demo-redis,
    and demo-nginx containers using stress-ng. This produces REAL cgroup
    CPU/memory pressure that Prometheus scrapes and the GNN reasons about.

  Enhancement 2 — Continuous Chaos Engineering (Chaos Monkey):
    During off-peak hours (23:00–05:00 by default, configurable),
    the chaos scheduler runs autonomously, picking random fault/attack
    scenarios and injecting them without human supervision. The RL Guardian
    must detect and remediate each one. All results are logged to SQLite
    so you can measure RL agent improvement over time.

Usage:
  python simulator.py                        # normal mode
  python simulator.py --chaos-always         # chaos mode 24/7 (for testing)
  python simulator.py --interval 5           # emit every 5s
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime

level = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=level,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ccdt.simulator")

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
TOPIC_EBPF = os.getenv("KAFKA_TOPIC_EBPF",        "ccdt.ebpf.events")
TOPIC_INCIDENTS = os.getenv("KAFKA_TOPIC_INCIDENTS",    "ccdt.incidents")
DOCKER_SOCKET = os.getenv("DOCKER_SOCKET",            "/var/run/docker.sock")
CHAOS_START_H = int(os.getenv("CHAOS_START_HOUR",    "23"))
CHAOS_END_H = int(os.getenv("CHAOS_END_HOUR",      "5"))

NODES = [
    {"id": "api-gw",        "label": "API Gateway",
        "layer": "network",  "is_critical": True},
    {"id": "auth-svc",      "label": "Auth Service",
        "layer": "service",  "is_critical": False},
    {"id": "order-svc",     "label": "Order Service",
        "layer": "service",  "is_critical": True},
    {"id": "payment-svc",   "label": "Payment Service",
        "layer": "service",  "is_critical": True},
    {"id": "inventory-svc", "label": "Inventory Service",
        "layer": "service",  "is_critical": False},
    {"id": "notify-svc",    "label": "Notify Service",
        "layer": "service",  "is_critical": False},
    {"id": "postgres",      "label": "PostgreSQL",
        "layer": "data",     "is_critical": True},
    {"id": "redis",         "label": "Redis Cache",
        "layer": "data",     "is_critical": False},
    {"id": "kafka",         "label": "Kafka Broker",
        "layer": "system",   "is_critical": True},
    {"id": "monitoring",    "label": "VictoriaMetrics",
        "layer": "system",   "is_critical": False},
]
NODE_IDS = [n["id"] for n in NODES]

BASELINE = {
    "api-gw":        {"cpu": 28, "mem": 41, "error_rate": 0.002, "request_rate": 420, "latency_ms": 8,   "tcp_retx": 0.1},
    "auth-svc":      {"cpu": 22, "mem": 38, "error_rate": 0.001, "request_rate": 310, "latency_ms": 3,   "tcp_retx": 0.0},
    "order-svc":     {"cpu": 45, "mem": 55, "error_rate": 0.005, "request_rate": 280, "latency_ms": 22,  "tcp_retx": 0.5},
    "payment-svc":   {"cpu": 31, "mem": 42, "error_rate": 0.002, "request_rate": 95,  "latency_ms": 12,  "tcp_retx": 0.1},
    "inventory-svc": {"cpu": 18, "mem": 29, "error_rate": 0.001, "request_rate": 60,  "latency_ms": 5,   "tcp_retx": 0.0},
    "notify-svc":    {"cpu": 25, "mem": 34, "error_rate": 0.001, "request_rate": 120, "latency_ms": 4,   "tcp_retx": 0.1},
    "postgres":      {"cpu": 38, "mem": 62, "error_rate": 0.003, "request_rate": 540, "latency_ms": 6,   "tcp_retx": 0.2},
    "redis":         {"cpu": 12, "mem": 45, "error_rate": 0.000, "request_rate": 820, "latency_ms": 1,   "tcp_retx": 0.0},
    "kafka":         {"cpu": 28, "mem": 51, "error_rate": 0.000, "request_rate": 380, "latency_ms": 2,   "tcp_retx": 0.0},
    "monitoring":    {"cpu": 15, "mem": 38, "error_rate": 0.000, "request_rate": 40,  "latency_ms": 1,   "tcp_retx": 0.0},
}

# Docker container names for real fault injection
CONTAINER_MAP = {
    "postgres":  "ccdt-demo-postgres-1",
    "redis":     "ccdt-demo-redis-1",
    "api-gw":    "ccdt-demo-nginx-1",
    "order-svc": "ccdt-demo-nginx-1",
}

SCENARIOS = [
    {
        "id": "oom_cascade", "type": "fault", "severity": "critical",
        "title": "PostgreSQL OOM Kill → Order Service TCP Storm",
        "description": "Memory pressure caused PostgreSQL OOM kill cascading to order-svc",
        "root_cause": "postgres", "affected": ["postgres", "order-svc", "notify-svc"],
        "duration_s": 120,
        "real_fault": {"container": "ccdt-demo-postgres-1", "stress": "memory", "intensity": "high"},
        "metrics": {
            "postgres":  {"cpu": 96, "mem": 94, "oom_count": 3, "tcp_retx": 0},
            "order-svc": {"cpu": 88, "mem": 81, "tcp_retx": 187, "latency_ms": 340, "error_rate": 0.18},
        },
        "ebpf_events": [
            {"type": "oom",   "pod": "postgres-0",
                "detail": "oom_kill: rss=6.2GB limit=6GB score=742"},
            {"type": "tcp",   "pod": "order-svc-7f8b",
                "detail": "tcp_retransmit_rate=187/s threshold=5/s"},
        ],
    },
    {
        "id": "cpu_saturation", "type": "fault", "severity": "critical",
        "title": "Order Service CPU Saturation → Cascading Timeouts",
        "description": "CPU throttling on order-svc causing upstream API gateway 503 cascade",
        "root_cause": "order-svc", "affected": ["order-svc", "api-gw", "payment-svc"],
        "duration_s": 90,
        "real_fault": {"container": "ccdt-demo-nginx-1", "stress": "cpu", "intensity": "high"},
        "metrics": {
            "order-svc":  {"cpu": 98, "mem": 72, "latency_ms": 420, "error_rate": 0.22},
            "api-gw":     {"cpu": 61, "mem": 58, "latency_ms": 380, "error_rate": 0.14},
        },
        "ebpf_events": [
            {"type": "sched", "pod": "order-svc-7f8b",
                "detail": "cpu_throttle=78% cgroup_limit_hit=true"},
        ],
    },
    {
        "id": "redis_eviction", "type": "fault", "severity": "warning",
        "title": "Redis Cache Eviction Storm — Order Query Flood",
        "description": "Redis maxmemory reached causing mass key eviction",
        "root_cause": "redis", "affected": ["redis", "order-svc"],
        "duration_s": 75,
        "real_fault": {"container": "ccdt-demo-redis-1", "stress": "memory", "intensity": "medium"},
        "metrics": {
            "redis":     {"cpu": 78, "mem": 98, "error_rate": 0.12, "latency_ms": 45},
            "order-svc": {"cpu": 62, "mem": 68, "error_rate": 0.08, "latency_ms": 120},
        },
        "ebpf_events": [
            {"type": "tcp", "pod": "redis-0",
                "detail": "evicted_keys=12847/s maxmemory_policy=allkeys-lru"},
        ],
    },
    {
        "id": "network_partition", "type": "fault", "severity": "critical",
        "title": "Network Partition — Payment Service Isolated",
        "description": "Network partition isolating payment-svc from postgres",
        "root_cause": "payment-svc", "affected": ["payment-svc", "postgres", "api-gw"],
        "duration_s": 80, "real_fault": None,
        "metrics": {
            "payment-svc": {"cpu": 42, "mem": 58, "tcp_retx": 340, "error_rate": 0.91, "latency_ms": 5000},
            "postgres":    {"cpu": 35, "mem": 61, "tcp_retx": 180, "error_rate": 0.31},
        },
        "ebpf_events": [
            {"type": "tcp", "pod": "payment-svc-5d9f",
                "detail": "connection_refused postgres:5432 340 retries"},
        ],
    },
    {
        "id": "kafka_lag", "type": "fault", "severity": "warning",
        "title": "Kafka Consumer Lag Accumulation — Notify Service",
        "description": "notify-svc Kafka consumer falling behind, lag >50k messages",
        "root_cause": "notify-svc", "affected": ["notify-svc", "kafka"],
        "duration_s": 100, "real_fault": None,
        "metrics": {
            "notify-svc": {"cpu": 91, "mem": 77, "latency_ms": 280, "error_rate": 0.09},
            "kafka":      {"cpu": 68, "mem": 74, "latency_ms": 25},
        },
        "ebpf_events": [
            {"type": "sched", "pod": "notify-svc-2a1c",
                "detail": "consumer_lag=51284 topic=ccdt.incidents"},
        ],
    },
    {
        "id": "privilege_escalation", "type": "attack", "severity": "critical",
        "title": "Privilege Escalation — CAP_SYS_ADMIN in Order Service",
        "description": "Attacker acquired CAP_SYS_ADMIN capability via container misconfiguration",
        "root_cause": "order-svc", "affected": ["order-svc", "postgres", "payment-svc"],
        "duration_s": 150, "real_fault": None,
        "metrics": {
            "order-svc":   {"cpu": 94, "mem": 78, "cap_events": 1, "file_events": 1, "syscall_rate": 8420},
            "postgres":    {"cpu": 91, "mem": 89, "oom_count": 2},
        },
        "ebpf_events": [
            {"type": "capability", "pod": "order-svc-7f8b",
                "detail": "cap_sys_admin SET uid=0 (root escalation)"},
            {"type": "syscall",    "pod": "order-svc-7f8b",
                "detail": "execve('/bin/xmrig') cryptominer binary"},
            {"type": "tcp",        "pod": "order-svc-7f8b",
                "detail": "outbound:4444 → 10.0.0.47 (C&C suspected)"},
        ],
    },
    {
        "id": "cryptominer", "type": "attack", "severity": "critical",
        "title": "Cryptominer Detected — Auth Service Compromised",
        "description": "Cryptomining process spawned in auth-svc after supply-chain injection",
        "root_cause": "auth-svc", "affected": ["auth-svc", "api-gw"],
        "duration_s": 130,
        "real_fault": {"container": "ccdt-demo-nginx-1", "stress": "cpu", "intensity": "max"},
        "metrics": {
            "auth-svc": {"cpu": 97, "mem": 82, "cap_events": 1, "syscall_rate": 12000, "file_events": 3},
            "api-gw":   {"cpu": 54, "mem": 49, "error_rate": 0.06},
        },
        "ebpf_events": [
            {"type": "syscall",    "pod": "auth-svc-5c9d",
                "detail": "execve('/tmp/.hidden/miner') uid=1000"},
            {"type": "tcp",        "pod": "auth-svc-5c9d",
                "detail": "outbound:3333 stratum+tcp (mining pool)"},
            {"type": "capability", "pod": "auth-svc-5c9d",
                "detail": "cap_net_raw SET — ARP spoofing risk"},
        ],
    },
    {
        "id": "lateral_movement", "type": "attack", "severity": "critical",
        "title": "Lateral Movement — Cross-Namespace Service Account Abuse",
        "description": "Compromised notify-svc attempting lateral movement",
        "root_cause": "notify-svc", "affected": ["notify-svc", "payment-svc", "api-gw"],
        "duration_s": 160, "real_fault": None,
        "metrics": {
            "notify-svc":  {"cpu": 72, "mem": 61, "cap_events": 1, "file_events": 8, "syscall_rate": 6200},
            "payment-svc": {"cpu": 58, "mem": 54, "error_rate": 0.12, "tcp_retx": 28},
        },
        "ebpf_events": [
            {"type": "file",       "pod": "notify-svc-2a1c",
                "detail": "read('/var/run/secrets/kubernetes.io/serviceaccount/token')"},
            {"type": "tcp",        "pod": "notify-svc-2a1c",
                "detail": "k8s_api:443 GET /api/v1/namespaces/payment/pods"},
            {"type": "capability", "pod": "notify-svc-2a1c",
                "detail": "cap_net_admin requested"},
        ],
    },
    {
        "id": "data_exfiltration", "type": "attack", "severity": "critical",
        "title": "Data Exfiltration — Postgres Dump via Compromised Inventory",
        "description": "Inventory service exfiltrating PostgreSQL dump to external IP",
        "root_cause": "inventory-svc", "affected": ["inventory-svc", "postgres", "api-gw"],
        "duration_s": 140, "real_fault": None,
        "metrics": {
            "inventory-svc": {"cpu": 88, "mem": 71, "file_events": 12, "tcp_retx": 4, "syscall_rate": 9800},
            "postgres":      {"cpu": 82, "mem": 79, "latency_ms": 340},
        },
        "ebpf_events": [
            {"type": "tcp",  "pod": "inventory-svc-3b2a",
                "detail": "outbound:22 → 185.220.101.47 (Tor exit node)"},
            {"type": "file", "pod": "inventory-svc-3b2a",
                "detail": "write('/tmp/dump.sql.gz') 847MB exfiltration"},
        ],
    },
    {
        "id": "brute_force", "type": "attack", "severity": "warning",
        "title": "Auth Service Brute Force — 847 Failed Logins/s",
        "description": "Credential stuffing attack targeting auth-svc login endpoint",
        "root_cause": "auth-svc", "affected": ["auth-svc", "api-gw"],
        "duration_s": 90, "real_fault": None,
        "metrics": {
            "auth-svc": {"cpu": 67, "mem": 59, "syscall_rate": 3400, "error_rate": 0.78, "tcp_retx": 8},
            "api-gw":   {"cpu": 58, "mem": 52, "error_rate": 0.31, "latency_ms": 240},
        },
        "ebpf_events": [
            {"type": "syscall", "pod": "auth-svc-5c9d",
                "detail": "getpeername()×847/s distributed IPs brute-force"},
        ],
    },
    {
        "id": "container_escape", "type": "attack", "severity": "critical",
        "title": "Container Escape Attempt — cgroup Breakout in Payment",
        "description": "Attacker attempting container escape via cgroup v1 breakout",
        "root_cause": "payment-svc", "affected": ["payment-svc", "postgres"],
        "duration_s": 120, "real_fault": None,
        "metrics": {
            "payment-svc": {"cpu": 84, "mem": 68, "cap_events": 2, "file_events": 15, "syscall_rate": 11200},
            "postgres":    {"cpu": 71, "mem": 75, "latency_ms": 180},
        },
        "ebpf_events": [
            {"type": "capability", "pod": "payment-svc-5d9f",
                "detail": "cap_sys_admin + cap_dac_read_search SET"},
            {"type": "file",       "pod": "payment-svc-5d9f",
                "detail": "open('/proc/1/root') host PID 1 access attempt"},
            {"type": "syscall",    "pod": "payment-svc-5d9f",
                "detail": "mount() cgroup breakout technique detected"},
        ],
    },
    {
        "id": "disk_io_saturation", "type": "fault", "severity": "warning",
        "title": "PostgreSQL Disk I/O Saturation — WAL Pressure",
        "description": "Write-ahead log flush latency spiking due to disk I/O saturation",
        "root_cause": "postgres", "affected": ["postgres", "order-svc"],
        "duration_s": 110,
        "real_fault": {"container": "ccdt-demo-postgres-1", "stress": "io", "intensity": "high"},
        "metrics": {
            "postgres":  {"cpu": 71, "mem": 78, "latency_ms": 840, "error_rate": 0.07, "tcp_retx": 8},
            "order-svc": {"cpu": 58, "mem": 62, "latency_ms": 620},
        },
        "ebpf_events": [
            {"type": "sched", "pod": "postgres-0",
                "detail": "io_wait=68% disk_write_latency_p99=840ms"},
            {"type": "probe", "pod": "order-svc-7f8b",
                "detail": "db_query_p99=620ms (normal=22ms)"},
        ],
    },
]


@dataclass
class NodeMetrics:
    cpu: float = 0.0
    mem: float = 0.0
    error_rate: float = 0.0
    request_rate: float = 0.0
    latency_ms: float = 0.0
    tcp_retx: float = 0.0
    oom_count: int = 0
    cap_events: int = 0
    syscall_rate: float = 0.0
    file_events: int = 0


class ClusterSimulator:
    def __init__(self, interval_s: float = 5.0, chaos_always: bool = False) -> None:
        self.interval_s = interval_s
        self.chaos_always = chaos_always
        self._rng = random.Random()
        self._metrics = {nid: NodeMetrics() for nid in NODE_IDS}
        self._scenario = None
        self._scenario_end = 0.0
        self._producer = None
        self._docker = None
        self._stress_proc_id = None
        self._incident_counter = 3000
        self._chaos_run_id = None

        for nid, base in BASELINE.items():
            m = self._metrics[nid]
            for k, v in base.items():
                setattr(m, k, v)

    # ── Connections ───────────────────────────────────────────────────────────

    async def _connect(self) -> None:
        try:
            from aiokafka import AIOKafkaProducer
            self._producer = AIOKafkaProducer(
                bootstrap_servers=KAFKA_BOOTSTRAP,
                value_serializer=lambda v: json.dumps(v).encode(),
            )
            await self._producer.start()
            logger.info("Kafka producer connected")
        except Exception as exc:
            logger.warning("Kafka unavailable (%s) — log-only mode", exc)

        try:
            import docker
            self._docker = docker.DockerClient(
                base_url=f"unix://{DOCKER_SOCKET}")
            self._docker.ping()
            logger.info("Docker API connected — real fault injection enabled")
        except Exception as exc:
            logger.warning(
                "Docker API unavailable (%s) — metric-only mode", exc)
            self._docker = None

    async def _publish(self, topic: str, msg: dict) -> None:
        if self._producer:
            try:
                await self._producer.send(topic, msg)
            except Exception:
                pass

    # ── Real fault injection ──────────────────────────────────────────────────

    def _inject_real_fault(self, fault_spec: dict) -> None:
        """
        Enhancement 1: inject a REAL fault into a Docker container via stress-ng.
        stress-ng runs as a one-shot command inside the target container,
        consuming real CPU/memory that cAdvisor measures and Prometheus scrapes.
        """
        if not self._docker or not fault_spec:
            return
        container_name = fault_spec.get("container")
        stress_type = fault_spec.get("stress", "cpu")
        intensity = fault_spec.get("intensity", "medium")

        intensity_map = {
            "low":    {"cpu": "--cpu 1 --timeout 30s",
                       "memory": "--vm 1 --vm-bytes 30% --timeout 30s",
                       "io":     "--io 1 --timeout 30s"},
            "medium": {"cpu": "--cpu 2 --timeout 60s",
                       "memory": "--vm 1 --vm-bytes 60% --timeout 60s",
                       "io":     "--io 2 --timeout 60s"},
            "high":   {"cpu": "--cpu 4 --timeout 90s",
                       "memory": "--vm 1 --vm-bytes 80% --timeout 90s",
                       "io":     "--io 4 --timeout 90s"},
            "max":    {"cpu": "--cpu 0 --timeout 120s",
                       "memory": "--vm 1 --vm-bytes 90% --timeout 120s",
                       "io":     "--io 8 --timeout 120s"},
        }
        args = intensity_map.get(
            intensity, intensity_map["medium"]).get(stress_type, "")

        try:
            container = self._docker.containers.get(container_name)
            result = container.exec_run(
                f"stress-ng {args}",
                detach=True,
                privileged=False,
            )
            self._stress_proc_id = result.id if hasattr(result, "id") else None
            logger.info("Real fault injected: stress-ng %s on %s (%s intensity)",
                        stress_type, container_name, intensity)
        except Exception as exc:
            try:
                if stress_type == "cpu":
                    container.exec_run(
                        "sh -c 'yes > /dev/null &'", detach=True)
                    logger.info(
                        "Fault injected via fallback (yes) on %s", container_name)
                elif stress_type == "memory":
                    container.exec_run(
                        "sh -c 'dd if=/dev/zero of=/tmp/fill bs=1M count=100 2>/dev/null &'", detach=True)
                    logger.info(
                        "Fault injected via fallback (dd) on %s", container_name)
                else:
                    logger.debug("Fault injection skipped ...")
            except Exception as e2:
                logger.debug("Fallback injection also failed ...")

    # ── Scenario management ───────────────────────────────────────────────────

    def _pick_scenario(self, chaos_mode: bool = False) -> None:
        new = self._rng.choice(SCENARIOS)
        while self._scenario and new["id"] == self._scenario["id"]:
            new = self._rng.choice(SCENARIOS)

        self._scenario = new
        duration = new["duration_s"] + self._rng.randint(-10, 10)
        self._scenario_end = time.monotonic() + duration
        self._incident_counter += 1
        inc_id = f"INC-{self._incident_counter}"

        prefix = "🤖 CHAOS" if chaos_mode else "🚨 NEW"
        logger.info("%s SCENARIO: [%s] %s (root=%s dur=%ds id=%s)",
                    prefix, new["type"].upper(), new["title"],
                    new["root_cause"], duration, inc_id)

        # Inject real fault if spec available
        if new.get("real_fault") and self._docker:
            self._inject_real_fault(new["real_fault"])

        asyncio.create_task(self._publish(TOPIC_INCIDENTS, {
            "msg_type":    "incident_created",
            "incident_id": inc_id,
            "title":       new["title"],
            "severity":    new["severity"],
            "type":        new["type"],
            "root_cause":  new["root_cause"],
            "affected":    new["affected"],
            "description": new["description"],
            "timestamp":   int(time.time()),
            "status":      "active",
            "chaos_run":   chaos_mode,
        }))

        # Enhancement 2: record chaos run in SQLite
        if chaos_mode:
            self._chaos_run_id = self._record_chaos_start(new, inc_id)

        return inc_id

    def _resolve_scenario(self) -> None:
        if not self._scenario:
            return
        logger.info("✅ RESOLVED: %s", self._scenario["title"])

        # Enhancement 2: close chaos run record
        if self._chaos_run_id:
            self._record_chaos_end(self._chaos_run_id)
            self._chaos_run_id = None

        self._scenario = None

    def _record_chaos_start(self, scenario: dict, inc_id: str) -> int | None:
        try:
            import sys
            sys.path.insert(0, "/app/shared")
            # Try to notify the API gateway to record in SQLite
            asyncio.create_task(self._publish(TOPIC_INCIDENTS, {
                "msg_type":      "chaos_run_start",
                "scenario_id":   scenario["id"],
                "scenario_title": scenario["title"],
                "type":          scenario["type"],
                "started_at":    int(time.time()),
                "incident_id":   inc_id,
            }))
            return int(time.time())  # use timestamp as run id
        except Exception:
            return None

    def _record_chaos_end(self, run_id: int) -> None:
        now = int(time.time())
        mttr = now - run_id
        asyncio.create_task(self._publish(TOPIC_INCIDENTS, {
            "msg_type":    "chaos_run_end",
            "started_at":  run_id,
            "resolved_at": now,
            "mttr_seconds": mttr,
            "scenario_id": self._scenario["id"] if self._scenario else "unknown",
        }))

    # ── Metric simulation ─────────────────────────────────────────────────────

    def _update_metrics(self) -> None:
        for nid, base in BASELINE.items():
            m = self._metrics[nid]
            def noise(s): return self._rng.gauss(0, s)
            m.cpu = max(2,   min(100, base["cpu"] + noise(4)))
            m.mem = max(5,   min(100, base["mem"] + noise(2)))
            m.error_rate = max(
                0,   min(1,   base["error_rate"] + noise(0.002)))
            m.request_rate = max(0,           base["request_rate"] + noise(20))
            m.latency_ms = max(0.5,         base["latency_ms"] + noise(2))
            m.tcp_retx = max(0,           base.get("tcp_retx", 0) + noise(0.1))
            m.oom_count = 0
            m.cap_events = 0
            m.file_events = 0
            m.syscall_rate = max(0,  base.get("syscall_rate", 100) + noise(50))

        if self._scenario and time.monotonic() < self._scenario_end:
            for nid, overrides in self._scenario.get("metrics", {}).items():
                if nid not in self._metrics:
                    continue
                m = self._metrics[nid]
                for metric, val in overrides.items():
                    if isinstance(val, float):
                        val = val + self._rng.gauss(0, val * 0.05)
                    setattr(m, metric, val)

    async def _emit_topology_update(self) -> None:
        nodes = []
        for nid in NODE_IDS:
            m = self._metrics.get(nid)
            if not m:
                continue
            nodes.append({
                "node_id": nid, "cpu": round(m.cpu, 1), "mem": round(m.mem, 1),
                "error_rate": round(m.error_rate, 4), "request_rate": round(m.request_rate, 1),
                "latency_ms": round(m.latency_ms, 1), "tcp_retx": round(m.tcp_retx, 2),
                "oom_count": m.oom_count, "cap_events": m.cap_events,
                "syscall_rate": round(m.syscall_rate, 1), "file_events": m.file_events,
            })
        await self._publish(TOPIC_EBPF, {
            "msg_type": "topology_update", "timestamp": int(time.time()), "nodes": nodes
        })

    async def _emit_ebpf_events(self) -> None:
        if not self._scenario:
            return
        events = self._scenario.get("ebpf_events", [])
        for evt in self._rng.sample(events, min(len(events), self._rng.randint(1, 3))):
            await self._publish(TOPIC_EBPF, {
                "msg_type": "ebpf_event", "timestamp": int(time.time()),
                "event_type": evt["type"], "pod": evt["pod"],
                "node": f"node-{self._rng.randint(1,3)}", "detail": evt["detail"],
                "severity": self._scenario["severity"], "scenario_id": self._scenario["id"],
            })

    # ── Chaos scheduler ───────────────────────────────────────────────────────

    def _is_chaos_hour(self) -> bool:
        """Enhancement 2: return True during configured off-peak hours."""
        if self.chaos_always:
            return True
        hour = datetime.now().hour
        if CHAOS_START_H > CHAOS_END_H:
            return hour >= CHAOS_START_H or hour < CHAOS_END_H
        return CHAOS_START_H <= hour < CHAOS_END_H

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        await self._connect()
        logger.info("Calm period (20s) ...")
        await asyncio.sleep(20)
        self._pick_scenario(chaos_mode=self._is_chaos_hour())

        while True:
            try:
                if self._scenario and time.monotonic() > self._scenario_end:
                    self._resolve_scenario()
                    calm = self._rng.randint(30, 60)
                    logger.info("Calm period: %ds ...", calm)
                    await asyncio.sleep(calm)
                    self._pick_scenario(chaos_mode=self._is_chaos_hour())

                if not self._scenario:
                    self._pick_scenario(chaos_mode=self._is_chaos_hour())

                self._update_metrics()
                await self._emit_topology_update()
                await self._emit_ebpf_events()

                if self._scenario:
                    root = self._scenario["root_cause"]
                    m = self._metrics.get(root)
                    remaining = max(
                        0, int(self._scenario_end - time.monotonic()))
                    chaos_tag = " 🤖CHAOS" if self._is_chaos_hour() else ""
                    logger.info("[%s] %s | root=%s cpu=%.0f%% mem=%.0f%% | %ds remaining%s",
                                self._scenario["severity"].upper(
                                ), self._scenario["id"],
                                root, m.cpu if m else 0, m.mem if m else 0,
                                remaining, chaos_tag)

            except Exception as exc:
                logger.error("Simulator tick error: %s", exc, exc_info=True)

            await asyncio.sleep(self.interval_s)


async def main() -> None:
    parser = argparse.ArgumentParser(description="CCDT Cluster Simulator")
    parser.add_argument("--interval",     type=float, default=5.0)
    parser.add_argument("--chaos-always", action="store_true",
                        help="Run chaos mode 24/7 (default: only during off-peak hours)")
    args = parser.parse_args()

    sim = ClusterSimulator(interval_s=args.interval,
                           chaos_always=args.chaos_always)
    logger.info("Simulator starting | interval=%.1fs | chaos_always=%s",
                args.interval, args.chaos_always)
    await sim.run()


if __name__ == "__main__":
    asyncio.run(main())

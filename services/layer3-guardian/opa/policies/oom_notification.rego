# CCDT Guardian — OOM Kill Notification Policy
# ═══════════════════════════════════════════════════════════════════════════════
# Governs remediation actions triggered by OOM kill events.
#
# Rules:
#   1. increase_oom_threshold is allowed automatically ONLY when:
#      - OOM kill count is >= MIN_OOM_KILLS_FOR_AUTO (1)
#      - The new memory limit does not exceed NODE_MEM_MAX_PCT of node capacity
#      - The requesting node is not a stateless service (stateful workloads need
#        careful memory management — require human approval)
#   2. For stateful workloads (postgres, mysql, redis, kafka, elasticsearch)
#      ALL memory-affecting actions require human approval (supervised mode)
#   3. Notification is required for any OOM event on a data-layer node —
#      this rule emits a `notify_required` flag for the executor to act on
#   4. Multiple OOM kills (>= CRITICAL_OOM_COUNT) within OOM_WINDOW_MINUTES
#      trigger an automatic escalation override regardless of autonomy mode
#
# Input schema:
#   input.node.oom_kills          int   OOM kills observed
#   input.node.mem                float current memory utilisation (0-100)
#   input.node.layer              string
#   input.action.parameters.new_mem_limit_gb  float
#   input.cluster.node_mem_total_gb           float  total node RAM
#   input.context.autonomy_mode   string

package ccdt.guardian.policies.oom_notification

import rego.v1

# ── Configuration ─────────────────────────────────────────────────────────────
MIN_OOM_KILLS_FOR_AUTO  := 1    # minimum OOM kills to trigger auto increase_oom_threshold
CRITICAL_OOM_COUNT      := 5    # OOM count that overrides autonomy to escalate
OOM_WINDOW_MINUTES      := 15
NODE_MEM_MAX_PCT        := 90   # max memory limit as % of node total RAM

# Stateful workload identifiers (by label prefix)
stateful_workload_prefixes := {"postgres", "mysql", "redis", "kafka",
                                "elasticsearch", "cassandra", "mongodb",
                                "zookeeper", "etcd"}

# ── Default deny ──────────────────────────────────────────────────────────────
default allow := false

# ── allow: increase_oom_threshold on stateless service with OOM kills ─────────
allow if {
    input.action.name == "increase_oom_threshold"
    input.node.oom_kills >= MIN_OOM_KILLS_FOR_AUTO
    not is_stateful_workload
    not mem_limit_exceeds_max
    not critical_oom_storm
}

# ── allow: restart_pod is safe for stateful workloads with memory pressure ────
# Restarting a stateful service is much safer than changing memory limits
# Allow autonomous restart for memory pressure scenarios on stateful workloads
# Either: explicit OOM kills detected, OR high memory usage (>85%)
allow if {
    input.action.name == "restart_pod"
    is_stateful_workload
    has_memory_pressure
    not critical_oom_storm
}

has_memory_pressure if {
    input.node.oom_kills >= MIN_OOM_KILLS_FOR_AUTO
}

has_memory_pressure if {
    input.node.mem >= 85
}

# ── allow: non-memory actions pass through this policy (other policies apply) ──
allow if {
    not input.action.name in {"increase_oom_threshold", "restart_pod", "drain_node"}
}

# ── allow: supervised or human-in-loop mode with explicit approval ─────────────
allow if {
    input.action.name in {"increase_oom_threshold", "restart_pod"}
    is_stateful_workload
    input.context.autonomy_mode in {"human-in-loop", "supervised"}
    input.context.human_approved == true
}

# ── Violations ────────────────────────────────────────────────────────────────
violations contains msg if {
    input.action.name == "increase_oom_threshold"
    input.node.oom_kills < MIN_OOM_KILLS_FOR_AUTO
    msg := sprintf(
        "oom_notification: increase_oom_threshold requires at least %v OOM kill(s). Node '%v' has %v.",
        [MIN_OOM_KILLS_FOR_AUTO, input.action.target_node, input.node.oom_kills]
    )
}

violations contains msg if {
    input.action.name == "increase_oom_threshold"
    is_stateful_workload
    not input.context.human_approved == true
    msg := sprintf(
        "oom_notification: node '%v' is a stateful workload. Memory limit changes require human approval. Current autonomy mode: '%v'.",
        [input.action.target_node, input.context.autonomy_mode]
    )
}

violations contains msg if {
    mem_limit_exceeds_max
    new_limit := input.action.parameters.new_mem_limit_gb
    total     := input.cluster.node_mem_total_gb
    msg := sprintf(
        "oom_notification: requested memory limit %.1f GB exceeds %v%% of node capacity (%.1f GB)",
        [new_limit, NODE_MEM_MAX_PCT, total * NODE_MEM_MAX_PCT / 100]
    )
}

violations contains msg if {
    critical_oom_storm
    msg := sprintf(
        "oom_notification: OOM storm detected (%v kills in %v min). Automatic escalation to human required regardless of autonomy mode.",
        [input.node.oom_kills, OOM_WINDOW_MINUTES]
    )
}

# ── Notification flags ────────────────────────────────────────────────────────
# These are read by the executor to trigger Slack/PagerDuty alerts

notify_required if {
    input.node.layer == "data"
    input.node.oom_kills >= 1
}

notify_required if {
    critical_oom_storm
}

escalate_required if {
    critical_oom_storm
}

# ── Helper rules ──────────────────────────────────────────────────────────────
is_stateful_workload if {
    some prefix in stateful_workload_prefixes
    startswith(input.action.target_node, prefix)
}

mem_limit_exceeds_max if {
    input.action.name == "increase_oom_threshold"
    input.action.parameters.new_mem_limit_gb != null
    input.cluster.node_mem_total_gb != null
    (input.action.parameters.new_mem_limit_gb / input.cluster.node_mem_total_gb) * 100 > NODE_MEM_MAX_PCT
}

critical_oom_storm if {
    input.node.oom_kills >= CRITICAL_OOM_COUNT
}

# ── Audit ──────────────────────────────────────────────────────────────────────
audit := {
    "policy":          "oom_notification",
    "action":          input.action.name,
    "target_node":     input.action.target_node,
    "node_oom_kills":  object.get(input.node, "oom_kills", 0),
    "is_stateful":     is_stateful_workload,
    "notify_required": notify_required,
    "result":          allow,
    "violations":      violations,
}

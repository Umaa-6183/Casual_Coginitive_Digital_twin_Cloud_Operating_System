# CCDT Guardian — CPU Threshold Safety Policy
# ═══════════════════════════════════════════════════════════════════════════════
# Prevents remediation actions that would starve a service of CPU to the
# point of unavailability, or that throttle an already under-loaded service.
#
# Rules:
#   1. scale_down_replicas is blocked when the service CPU is already below
#      MIN_CPU_PCT_THRESHOLD (20%) — scaling down would waste capacity
#   2. throttle_cpu is blocked when target CPU is below THROTTLE_MIN (30%)
#      or when the service is classified as "data" layer (DBs must not throttle)
#   3. throttle_cpu is blocked when quota would drop below CPU_FLOOR (0.1 cores)
#   4. cordon_node / drain_node are blocked when the node is the ONLY node
#      running a critical system-layer service
#
# Input: same schema as no_privilege_escalation.rego

package ccdt.guardian.policies.cpu_threshold

import rego.v1

# ── Thresholds ────────────────────────────────────────────────────────────────
MIN_CPU_PCT_THRESHOLD := 20   # % — below this, scaling down is wasteful / risky
THROTTLE_MIN_CPU_PCT  := 30   # % — don't throttle services already at low CPU
CPU_FLOOR_CORES       := 0.1  # minimum CPU cores after throttle

# Data-layer services that must NEVER be CPU-throttled
protected_layers := {"data", "system"}

# ── Default deny ──────────────────────────────────────────────────────────────
default allow := false

# ── Allow ─────────────────────────────────────────────────────────────────────
allow if {
    not scale_down_below_threshold
    not throttle_underloaded
    not throttle_protected_layer
    not throttle_below_floor
    not drain_last_system_node
}

# ── Violations ────────────────────────────────────────────────────────────────
violations contains msg if {
    scale_down_below_threshold
    cpu := input.node.cpu
    msg := sprintf(
        "scale_down_replicas blocked: node '%v' CPU is already %.0f%% (below %v%% threshold)",
        [input.action.target_node, cpu, MIN_CPU_PCT_THRESHOLD]
    )
}

violations contains msg if {
    throttle_underloaded
    cpu := input.node.cpu
    msg := sprintf(
        "throttle_cpu blocked: node '%v' CPU %.0f%% is already below throttle minimum %v%%",
        [input.action.target_node, cpu, THROTTLE_MIN_CPU_PCT]
    )
}

violations contains msg if {
    throttle_protected_layer
    layer := input.node.layer
    msg := sprintf(
        "throttle_cpu blocked: node '%v' is in protected layer '%v' (CPU throttling not allowed)",
        [input.action.target_node, layer]
    )
}

violations contains msg if {
    throttle_below_floor
    requested := input.action.parameters.cpu_limit_cores
    msg := sprintf(
        "throttle_cpu blocked: requested CPU limit %.2f cores is below floor %.2f cores",
        [requested, CPU_FLOOR_CORES]
    )
}

violations contains msg if {
    drain_last_system_node
    msg := sprintf(
        "drain_node / cordon_node blocked: '%v' is the last running node for a system-layer service",
        [input.action.target_node]
    )
}

# ── Helper rules ──────────────────────────────────────────────────────────────
scale_down_below_threshold if {
    input.action.name == "scale_down_replicas"
    input.node.cpu < MIN_CPU_PCT_THRESHOLD
}

throttle_underloaded if {
    input.action.name == "throttle_cpu"
    input.node.cpu < THROTTLE_MIN_CPU_PCT
}

throttle_protected_layer if {
    input.action.name == "throttle_cpu"
    input.node.layer in protected_layers
}

throttle_below_floor if {
    input.action.name == "throttle_cpu"
    input.action.parameters.cpu_limit_cores < CPU_FLOOR_CORES
}

drain_last_system_node if {
    input.action.name in {"drain_node", "cordon_node"}
    input.node.layer == "system"
    # Only one node of this service type is running
    count([n | n := input.cluster.nodes[_]; n.layer == "system"; n.status != "drained"]) == 1
}

# ── Audit ──────────────────────────────────────────────────────────────────────
audit := {
    "policy":      "cpu_threshold",
    "action":      input.action.name,
    "target_node": input.action.target_node,
    "node_cpu":    input.node.cpu,
    "node_layer":  input.node.layer,
    "result":      allow,
    "violations":  violations,
}

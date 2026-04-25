# CCDT Guardian — Lateral Movement Prevention Policy
# ═══════════════════════════════════════════════════════════════════════════════
# Detects and blocks remediation actions that could facilitate or enable
# lateral movement between namespaces, pods, or nodes.
#
# A lateral movement risk exists when:
#   1. A compromised node (class=attack) is the target of a rollback_deployment
#      action — rolling back could restore the attacker's persistent foothold
#   2. An action would grant the target pod access to another namespace's
#      service account token
#   3. A restart_pod action is attempted on an attack-classified node without
#      first isolating it — restart without isolation allows re-entry
#   4. The requesting identity (RBAC subject) does not have the right to
#      modify the target namespace
#   5. The same action has been attempted more than MAX_RETRY_COUNT times in
#      the past RETRY_WINDOW_MINUTES on the same node (prevents brute-force
#      remediation loops that an attacker could exploit)
#
# Input schema: same as no_privilege_escalation.rego
# Additional fields:
#   input.action.history          array of recent actions on this node
#   input.node.is_isolated        bool — whether node is already network-isolated
#   input.context.rbac_subject    string
#   input.context.can_write_ns    array of writable namespace names

package ccdt.guardian.policies.lateral_movement

import rego.v1

# ── Configuration ─────────────────────────────────────────────────────────────
MAX_RETRY_COUNT        := 3    # max same-action retries in window
RETRY_WINDOW_MINUTES   := 10

# Actions that must not target attack-classified nodes without isolation
requires_isolation_first := {
    "restart_pod",
    "rollback_deployment",
    "scale_up_replicas",
    "enable_debug_logging",
}

# ── Default deny ──────────────────────────────────────────────────────────────
default allow := false

# ── Allow ─────────────────────────────────────────────────────────────────────
allow if {
    not rollback_attack_node
    not restart_unisolated_attack_node
    not cross_namespace_privilege_creep
    not rbac_insufficient
    not action_retry_loop
}

# ── Violations ────────────────────────────────────────────────────────────────
violations contains msg if {
    rollback_attack_node
    msg := sprintf(
        "lateral_movement: rollback_deployment on attack-classified node '%v' is blocked. Rolling back may restore attacker's persistent implant. Isolate first.",
        [input.action.target_node]
    )
}

violations contains msg if {
    restart_unisolated_attack_node
    msg := sprintf(
    "lateral_movement: '%v' on attack-classified node '%v' blocked. Node must be network-isolated (action: isolate_container) before restart/rollback.",
    [input.action.name, input.action.target_node]
    )
}

violations contains msg if {
    cross_namespace_privilege_creep
    target_ns := input.action.parameters.target_namespace
    msg := sprintf(
        "lateral_movement: action '%v' would grant access across namespace boundary to '%v'",
        [input.action.name, target_ns]
    )
}

violations contains msg if {
    rbac_insufficient
    subj := input.context.rbac_subject
    ns   := input.cluster.namespace
    msg := sprintf(
        "lateral_movement: RBAC subject '%v' does not have write access to namespace '%v'",
        [subj, ns]
    )
}

violations contains msg if {
    action_retry_loop
    count_recent := count_recent_same_actions
    msg := sprintf(
    "lateral_movement: action '%v' on '%v' has been attempted %v times in %v minutes (max %v). Possible remediation loop — requires human review.",
    [input.action.name, input.action.target_node,
     count_recent, RETRY_WINDOW_MINUTES, MAX_RETRY_COUNT]
    )
}
# ── Helper rules ──────────────────────────────────────────────────────────────
rollback_attack_node if {
    input.action.name == "rollback_deployment"
    input.node.class == "attack"
}

restart_unisolated_attack_node if {
    input.action.name in requires_isolation_first
    input.node.class == "attack"
    not input.node.is_isolated == true
}

cross_namespace_privilege_creep if {
    input.action.parameters.target_namespace != null
    input.action.parameters.target_namespace != input.cluster.namespace
    not input.action.parameters.cross_namespace_approved == true
}

rbac_insufficient if {
    input.context.rbac_subject != null
    input.context.can_write_ns != null
    not input.cluster.namespace in input.context.can_write_ns
}

action_retry_loop if {
    count_recent_same_actions > MAX_RETRY_COUNT
}

count_recent_same_actions := count([h |
    h := input.action.history[_]
    h.action_name == input.action.name
    h.target_node == input.action.target_node
    # within the retry window (history entries have age_minutes field)
    h.age_minutes <= RETRY_WINDOW_MINUTES
])

# ── Special case: isolation itself is always permitted on attack nodes ─────────
# (overrides restart_unisolated_attack_node for the isolate action itself)
allow if {
    input.action.name == "isolate_container"
    input.node.class == "attack"
}

# ── Audit ──────────────────────────────────────────────────────────────────────
audit := {
    "policy":        "lateral_movement",
    "action":        input.action.name,
    "target_node":   input.action.target_node,
    "node_class":    input.node.class,
    "is_isolated":   object.get(input.node, "is_isolated", false),
    "rbac_subject":  object.get(input.context, "rbac_subject", "unknown"),
    "result":        allow,
    "violations":    violations,
}

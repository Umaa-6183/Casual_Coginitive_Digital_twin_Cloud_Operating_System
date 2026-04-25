# CCDT Guardian — No Privilege Escalation Policy
# ═══════════════════════════════════════════════════════════════════════════════
# Blocks any remediation action that would result in a container running
# with elevated Linux capabilities (CAP_SYS_ADMIN, CAP_NET_ADMIN, etc.)
# or in privileged mode.
#
# This policy enforces the Principle of Least Privilege at the remediation
# layer. Even if the RL agent proposes an action that technically resolves
# an incident, it is blocked if it would introduce privilege escalation.
#
# Input schema:
#   input.action.name          string   action being requested
#   input.action.target_node   string   node ID being acted on
#   input.action.parameters    object   action-specific parameters
#   input.node                 object   target node state from topology
#   input.cluster              object   cluster-level metadata
#   input.context.user         string   user or system requesting action
#   input.context.autonomy_mode string  human-in-loop|supervised|full-auto
#
# Output:
#   allow                      boolean  true = action permitted
#   violations                 set      reasons why action is denied

package ccdt.guardian.policies.no_privilege_escalation

import rego.v1

# ── Dangerous capabilities that must never be granted automatically ──────────
dangerous_capabilities := {
    "CAP_SYS_ADMIN",
    "CAP_SYS_PTRACE",
    "CAP_SYS_MODULE",
    "CAP_SYS_RAWIO",
    "CAP_SYS_BOOT",
    "CAP_NET_ADMIN",
    "CAP_SETUID",
    "CAP_SETGID",
    "CAP_DAC_OVERRIDE",
}

# ── Actions that are always blocked regardless of context ────────────────────
always_blocked_actions := {
    "grant_privileged_mode",
    "add_capability",
    "modify_security_context",
    "run_as_root",
}

# ── Default deny ──────────────────────────────────────────────────────────────
default allow := false

# ── Allow: action is not in the blocked list and has no cap requests ──────────
allow if {
    not action_always_blocked
    not requests_dangerous_capability
    not escalates_to_root
    not modifies_pod_security_policy
}

# ── Violations ────────────────────────────────────────────────────────────────
violations contains msg if {
    action_always_blocked
    msg := sprintf(
        "Action '%v' is unconditionally blocked by no_privilege_escalation policy",
        [input.action.name]
    )
}

violations contains msg if {
    requests_dangerous_capability
    cap := input.action.parameters.capability
    msg := sprintf(
        "Action '%v' requests dangerous capability '%v' which is not permitted",
        [input.action.name, cap]
    )
}

violations contains msg if {
    escalates_to_root
    msg := sprintf(
        "Action '%v' would run container as uid=0 (root) which is not permitted",
        [input.action.name]
    )
}

violations contains msg if {
    modifies_pod_security_policy
    msg := sprintf(
        "Action '%v' attempts to modify PodSecurityPolicy — requires human approval",
        [input.action.name]
    )
}

# ── Helper rules ──────────────────────────────────────────────────────────────
action_always_blocked if {
    input.action.name in always_blocked_actions
}

requests_dangerous_capability if {
    cap := input.action.parameters.capability
    cap in dangerous_capabilities
}

escalates_to_root if {
    input.action.parameters.run_as_user == 0
}

escalates_to_root if {
    input.action.parameters.privileged == true
}

modifies_pod_security_policy if {
    input.action.parameters.modify_psp == true
}

# ── Audit annotation ──────────────────────────────────────────────────────────
# Emitted to audit log regardless of allow/deny outcome.
audit := {
    "policy":      "no_privilege_escalation",
    "action":      input.action.name,
    "target_node": input.action.target_node,
    "result":      allow,
    "violations":  violations,
    "timestamp":   time.now_ns(),
}

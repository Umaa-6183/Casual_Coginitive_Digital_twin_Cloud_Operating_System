# CCDT Guardian — Egress Control Policy
# ═══════════════════════════════════════════════════════════════════════════════
# Enforces egress (outbound network) controls on remediation actions.
#
# Rules:
#   1. apply_network_policy requires the target namespace to be in the
#      approved namespaces list (production namespaces only)
#   2. Deny network policy changes that would open egress to the internet
#      (CIDR 0.0.0.0/0 or ::/0)
#   3. Deny egress rules that target non-RFC-1918 addresses without explicit
#      exemption
#   4. In full-auto autonomy mode, network policy changes always require
#      at minimum supervised approval (never fully automatic)
#   5. Log all egress-related actions regardless of allow/deny outcome
#
# Input schema: same as no_privilege_escalation.rego
# Additional fields:
#   input.action.parameters.egress_rules   array of CIDR/port rules
#   input.cluster.namespace                target namespace
#   input.context.autonomy_mode            human-in-loop|supervised|full-auto

package ccdt.guardian.policies.egress_control

import rego.v1

# ── Configuration ─────────────────────────────────────────────────────────────
approved_namespaces := {
    "production",
    "prod",
    "staging",
    "default",
    "kube-system",
    "monitoring",
    "ccdt",
}

# RFC-1918 private CIDR prefixes — always allowed for internal traffic
private_cidr_prefixes := {"10.", "172.16.", "172.17.", "172.18.", "172.19.",
                           "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
                           "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
                           "172.30.", "172.31.", "192.168."}

# Internet-facing CIDRs that are never allowed via automation
blocked_cidrs := {"0.0.0.0/0", "::/0", "0.0.0.0", "::"}

# Actions that affect network egress
egress_affecting_actions := {
    "apply_network_policy",
    "isolate_container",
}

# ── Default deny ──────────────────────────────────────────────────────────────
default allow := false

# ── Allow ─────────────────────────────────────────────────────────────────────
allow if {
    not unapproved_namespace
    not opens_internet_egress
    not non_rfc1918_without_exemption
    not full_auto_network_change
}

# ── Violations ────────────────────────────────────────────────────────────────
violations contains msg if {
    unapproved_namespace
    ns := input.cluster.namespace
    msg := sprintf(
        "egress_control: namespace '%v' is not in the approved namespace list for network policy changes",
        [ns]
    )
}

violations contains msg if {
    opens_internet_egress
    some rule in object.get(input.action.parameters, "egress_rules", [])
    rule.cidr in blocked_cidrs
    msg := sprintf(
        "egress_control: egress rule with CIDR '%v' opens unrestricted internet access — blocked",
        [rule.cidr]
    )
}

violations contains msg if {
    non_rfc1918_without_exemption
    msg := "egress_control: egress rule targets non-RFC-1918 address without explicit exemption"
}

violations contains msg if {
    full_auto_network_change
    msg := sprintf(
        "egress_control: network policy changes are not permitted in full-auto mode. Current autonomy mode: '%v'. Requires supervised or human-in-loop.",
        [input.context.autonomy_mode]
    )
}

# ── Helper rules ──────────────────────────────────────────────────────────────
unapproved_namespace if {
    input.action.name in egress_affecting_actions
    not input.cluster.namespace in approved_namespaces
}

opens_internet_egress if {
    input.action.name == "apply_network_policy"
    some rule in object.get(input.action.parameters, "egress_rules", [])
    rule.cidr in blocked_cidrs
}

non_rfc1918_without_exemption if {
    input.action.name == "apply_network_policy"
    some rule in object.get(input.action.parameters, "egress_rules", [])
    not is_private_cidr(rule.cidr)
    not rule.exempted == true
}

full_auto_network_change if {
    input.action.name in egress_affecting_actions
    input.context.autonomy_mode == "full-auto"
}

is_private_cidr(cidr) if {
    some prefix in private_cidr_prefixes
    startswith(cidr, prefix)
}

# ── Audit ──────────────────────────────────────────────────────────────────────
audit := {
    "policy":        "egress_control",
    "action":        input.action.name,
    "target_node":   input.action.target_node,
    "namespace":     input.cluster.namespace,
    "autonomy_mode": input.context.autonomy_mode,
    "result":        allow,
    "violations":    violations,
}

class Decision:
    AUTO = "AUTO"
    APPROVE = "APPROVE"
    DENY = "DENY"


def policy_check(tool_name, tool_args):
    """Phase 2 placeholder: every call is auto-approved.

    Replaced in Phase 3 by the real tiered policy (AUTO/APPROVE/DENY per
    tool, decided at definition time) plus the escalation heuristic that
    forces write actions into APPROVE when the issue body looks like a
    prompt injection attempt.
    """
    return Decision.AUTO, None

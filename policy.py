import re

import tools

# Tiers are fixed here, at definition time -- never computed from model
# output or read out of the system prompt.
AUTO_TOOLS = {"list_issues", "get_issue", "search_issues", "add_labels", "post_comment"}
APPROVE_TOOLS = {"assign_issue", "close_issue"}
DENY_TOOLS = {"delete_comment"}

# Only these AUTO-tier tools get escalated to APPROVE when the issue body
# looks like a prompt injection attempt. add_labels deliberately stays
# AUTO always: mislabeling is low-consequence and easily reversible, so
# it isn't worth the human-review friction even under suspected injection.
ESCALATABLE_TOOLS = {"post_comment"}

INJECTION_PATTERNS = [
    re.compile(r"\bignore\b", re.IGNORECASE),
    re.compile(r"\byou must\b", re.IGNORECASE),
    re.compile(r"^\s*system\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bdisregard\b", re.IGNORECASE),
]


class Decision:
    AUTO = "AUTO"
    APPROVE = "APPROVE"
    DENY = "DENY"


def _looks_like_injection(text):
    return any(pattern.search(text or "") for pattern in INJECTION_PATTERNS)


def _is_issue_escalated(issue_number):
    """Deliberately uncached: the issue body is re-fetched on every check.

    A cache keyed by issue number would let someone file a benign issue,
    wait for the clean result to be cached, then edit the body to add
    injection text -- the stale "not escalated" verdict would stand. The
    extra API call is cheap next to that.
    """
    issue = tools.get_issue(issue_number)
    return _looks_like_injection(issue["title"]) or _looks_like_injection(issue["body"])


def policy_check(tool_name, tool_args):
    """The dispatch-layer enforcement point.

    Called by the agent loop with only the tool name and its arguments --
    never the model's reasoning, the system prompt, or the issue text
    itself. Text injected into an issue body can steer what the model
    decides to call; it cannot change what this function returns, because
    this function never reads the model's output at all.
    """
    if tool_name in DENY_TOOLS:
        return Decision.DENY, f"{tool_name} is permanently denied and never dispatched"

    if tool_name not in AUTO_TOOLS and tool_name not in APPROVE_TOOLS:
        return Decision.DENY, f"{tool_name} is not a recognized tool"

    if tool_name in APPROVE_TOOLS:
        return Decision.APPROVE, "this action always requires human approval"

    if tool_name in ESCALATABLE_TOOLS:
        issue_number = tool_args.get("issue_number")
        if issue_number is not None and _is_issue_escalated(issue_number):
            return Decision.APPROVE, "issue body contains instruction-like text; escalated to human approval"

    return Decision.AUTO, None

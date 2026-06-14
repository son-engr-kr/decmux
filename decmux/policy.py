"""Auto-vs-escalate policy for Feed decisions (pure, testable).

Conservative by default: a decision is only 'auto' if it is a permission request
for a reversible/safe tool on the allowlist. Strategic decisions (AskUserQuestion,
ExitPlanMode, Notification) always escalate. Whether an 'auto' verdict actually
fires is decided by the session (auto-answer can be turned off); when it fires it
replies "once", never granting a standing permission.
"""

from __future__ import annotations

# Reversible / safe tools that may be auto-approved.
SAFE_TOOLS = {
    "Read", "Glob", "Grep", "LS", "NotebookRead",
    "TodoWrite", "WebFetch", "WebSearch", "BashOutput",
}


def decide(*, hook_event: str | None = None, tool_name: str | None = None,
           allow: set[str] | None = None) -> str:
    """Return 'auto' (safe/reversible) or 'escalate' (human/manager call)."""
    he = (hook_event or "").lower()
    if "plan" in he or "question" in he or "notification" in he:
        return "escalate"   # strategic / human-intent
    allowset = allow if allow is not None else SAFE_TOOLS
    if tool_name and tool_name in allowset:
        return "auto"
    return "escalate"

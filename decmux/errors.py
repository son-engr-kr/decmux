"""Detect error / usage-limit states from terminal text (pure, testable).

The classifier reads an agent's whole visible screen, which includes the agent's
own prose. Tokens must therefore be *self-framing*: a manager surface discussing
whether its agents are "overloaded", or an ML log printing "500 steps" /
"429 episodes", must NOT be read as an error/limit state. So bare words and bare
HTTP numbers are out — we match the literal banners agents print ("API Error",
``overloaded_error``) and accept HTTP status codes only when an error/status word
frames them (e.g. "API Error: 529", "status 500").
"""

from __future__ import annotations

import re

# An HTTP status code only counts when an error/status word sits just before it,
# so an ordinary number in agent output ("500 steps") is not mistaken for a 5xx.
_FRAME = r"(?:api[ -]?error|error|status|http)\W{0,12}"

# Usage/rate limits -> "budget".
_BUDGET = re.compile(
    r"usage limit|session limit|rate.?limit|quota exceeded|too many requests"
    r"|resets?(?: at)?\s+\d{1,2}:\d{2}\s*(?:am|pm)?"
    r"|limit reached|usage-credits|upgrade to (?:increase|continue)"
    r"|" + _FRAME + r"\b429\b",
    re.IGNORECASE,
)
# Transient/API errors -> "error".
_ERROR = re.compile(
    r"\bapi error\b|overloaded_error|internal server error"
    r"|connection error|request failed|fetch failed|econnreset"
    r"|" + _FRAME + r"\b5(?:00|02|03|29)\b",
    re.IGNORECASE,
)


def detect(text: str) -> str | None:
    """Return 'budget', 'error', or None for the given recent terminal text."""
    if not text:
        return None
    if _BUDGET.search(text):
        return "budget"
    if _ERROR.search(text):
        return "error"
    return None


# Usage limit, parsed from the agent's screen: Claude prints "N% used", Codex prints
# "N% left". Context-window indicators ("context left", "until auto-compact") use the
# same shape, so we drop any % framed by those words.
_USAGE_PCT = re.compile(r"(\d{1,3})\s*%\s*(used|left)\b", re.IGNORECASE)


def parse_usage_used(text: str) -> float | None:
    """Percent of the usage limit CONSUMED (0–100), or None if not shown. 'N% left'
    is normalized to used = 100 − N. Returns the most-constraining (max) value so the
    binding limit (e.g. Codex's 5h vs weekly) is the one surfaced."""
    if not text:
        return None
    best: float | None = None
    for m in _USAGE_PCT.finditer(text):
        pre = text[max(0, m.start() - 24):m.start()].lower()
        if "context" in pre or "compact" in pre:
            continue                      # a context-window %, not the usage limit
        n = int(m.group(1))
        if not 0 <= n <= 100:
            continue
        used = float(n) if m.group(2).lower() == "used" else 100.0 - n
        best = used if best is None else max(best, used)
    return best

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

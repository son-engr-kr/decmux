"""Error/usage-limit detection must be self-framing (no false positives on prose)."""

from __future__ import annotations

from decmux.errors import detect


def test_self_framing_positives():
    assert detect("Usage limit reached") == "budget"
    assert detect("rate limit exceeded") == "budget"
    assert detect("resets at 3:00pm") == "budget"
    assert detect("API Error: 529") == "error"
    assert detect("overloaded_error") == "error"
    assert detect("Internal Server Error") == "error"
    assert detect("status 500") == "error"


def test_bare_tokens_not_matched():
    assert detect("training for 500 steps") is None       # bare 5xx, no frame
    assert detect("429 episodes completed") is None        # bare 429, no frame
    assert detect("the agents are overloaded with work") is None  # 'overloaded' != overloaded_error
    assert detect("") is None


def test_parse_usage_used_claude_and_codex():
    from decmux import errors
    assert errors.parse_usage_used("ctx … 23% used … ") == 23.0          # claude: % used
    assert errors.parse_usage_used("5h limit: 40% left") == 60.0          # codex: % left -> used
    # most-constraining wins (codex 5h vs weekly)
    assert errors.parse_usage_used("5h 30% left   week 10% left") == 90.0
    # context-window % is NOT the usage limit
    assert errors.parse_usage_used("Context left until auto-compact: 12%") is None
    assert errors.parse_usage_used("nothing here") is None
    assert errors.parse_usage_used("") is None

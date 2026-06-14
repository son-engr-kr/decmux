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

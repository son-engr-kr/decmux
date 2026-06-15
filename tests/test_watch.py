"""Classification invariants (pure parts — no cmux needed)."""

from __future__ import annotations

from decmux.watch import (
    Watcher,
    _parse_model_effort,
    _runtime_shell,
    _screen_status,
    classify,
)


def test_classify_priority():
    # procs==0 -> dead, regardless of cpu
    assert classify(99.0, 0, None, 100.0, stuck_after=300, busy_cpu=1.0) == "dead"
    # cpu over threshold -> working
    assert classify(5.0, 2, 50.0, 100.0, stuck_after=300, busy_cpu=1.0) == "working"
    # never seen active -> idle
    assert classify(0.0, 2, None, 100.0, stuck_after=300, busy_cpu=1.0) == "idle"
    # quiet past stuck_after -> stuck
    assert classify(0.0, 2, 0.0, 400.0, stuck_after=300, busy_cpu=1.0) == "stuck"
    # quiet but within window -> idle
    assert classify(0.0, 2, 0.0, 100.0, stuck_after=300, busy_cpu=1.0) == "idle"


def test_screen_status_structural_spinner():
    assert _screen_status("... esc to interrupt") == "working"
    assert _screen_status("✶ Deciphering…") == "working"        # bare gerund spinner
    assert _screen_status("↑ 12k tokens") == "working"          # live token meter
    # a completed turn is past-tense, no ellipsis, no live meter -> idle
    assert _screen_status("Cogitated for 1m 51s") == "idle"
    # idle footer mentions tokens but has no ↑/↓ arrow -> idle
    assert _screen_status("/clear to save 512.4k tokens") == "idle"
    assert _screen_status("") is None


def test_screen_status_ignores_boxed_welcome_gerund():
    # Claude's welcome/changelog panel lives in a box and contains gerunds like
    # "caching…"; a freshly-spawned idle agent must NOT read as working.
    welcome = (
        "│ What's new                                  │\n"
        "│ Improved Bedrock credential caching…        │\n"
        "❯\n"
        "  Opus 4.8 (1M context) [xhigh]"
    )
    assert _screen_status(welcome) == "idle"


def test_runtime_shell_vs_login_shell():
    # a bash child of the agent runtime (claude) = a live Bash tool -> True
    rows = [("100", "surface:1", "claude"), ("200", "100", "bash")]
    assert _runtime_shell(rows) is True
    # the surface's own login shell (parent = surface, not runtime) -> False
    rows = [("100", "surface:1", "claude"), ("300", "surface:1", "zsh")]
    assert _runtime_shell(rows) is False
    # no runtime at all -> False
    assert _runtime_shell([("300", "surface:1", "zsh")]) is False


def test_hysteresis_commit():
    w = Watcher("ws")
    assert w._commit("k", "idle", 2) == "idle"        # first observation commits
    assert w._commit("k", "working", 2) == "idle"     # 1 poll of the new state: hold
    assert w._commit("k", "working", 2) == "working"  # 2 consecutive polls: commit


def test_hysteresis_flap_resets():
    w = Watcher("ws")
    w._commit("k", "idle", 2)
    assert w._commit("k", "working", 2) == "idle"     # candidate working, count 1
    assert w._commit("k", "stuck", 2) == "idle"       # candidate switched, count resets
    assert w._commit("k", "stuck", 2) == "stuck"      # now 2 consecutive


def test_model_effort_bottom_most_footer():
    text = "decoy opus 4.1 [low]\nclaude sonnet 4.6 [high]"
    model, effort = _parse_model_effort(text)
    assert model.startswith("sonnet")   # bottom-most match wins
    assert effort == "high"

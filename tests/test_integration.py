"""Agent integration: cmux-send guard, the SessionStart protocol hook, and purge."""

from __future__ import annotations

import json

import pytest

from decmux import assets, cli, cmux, hooks
from decmux.store import Store


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Redirect on-disk asset paths into tmp, with a fake cmux binary."""
    monkeypatch.setattr(cmux, "CMUX_BIN", "/usr/bin/cmux")
    monkeypatch.setattr(assets, "GUARD_DIR", tmp_path / "bin")
    monkeypatch.setattr(assets, "GUARD_CMUX", tmp_path / "bin" / "cmux")
    monkeypatch.setattr(hooks, "CLAUDE_SETTINGS", tmp_path / "settings.json")
    return tmp_path


# --- cmux-send guard ---

def test_guard_blocks_raw_input(sandbox):
    assets._ensure_cmux_guard()
    shim = (sandbox / "bin" / "cmux").read_text()
    assert "send|send-key" in shim and "decmux guard" in shim
    assert (sandbox / "bin" / "cmux").stat().st_mode & 0o111


def test_guarded_command_structure(sandbox):
    cmd = assets.guarded_command(
        "claude --foo",
        env={"DECMUX_ROLE": "agent", "CMUX_SURFACE_REF": "surface:1"}, cwd="/work")
    assert cmd.startswith("cd /work &&")
    assert "DECMUX_REAL_CMUX=" in cmd and "PATH=" in cmd
    assert "DECMUX_ROLE=agent" in cmd and "claude --foo" in cmd


# --- SessionStart hook (self-guarding; no skill file) ---

def test_setup_installs_self_guarding_hook(sandbox):
    cli.main(["setup"])
    assert hooks.claude_status()["session_start_hook"] is True
    cmd = json.loads((sandbox / "settings.json").read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "command -v decmux" in cmd and "|| true" in cmd     # inert when decmux is gone
    assert hooks.install_all_hooks()["session_hook"] is False  # idempotent


def test_setup_hint_when_no_hook(sandbox, capsys):
    cli._setup_hint()
    assert "decmux setup" in capsys.readouterr().out


# --- protocol injection is scoped to decmux sessions ---

def test_injects_for_spawned_agent(sandbox, monkeypatch, capsys):
    monkeypatch.setenv("DECMUX_ROLE", "agent")
    assert hooks.session_start() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert out["hookSpecificOutput"]["additionalContext"] == assets.PROTOCOL


def test_silent_for_normal_session(sandbox, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("DECMUX_ROLE", raising=False)
    monkeypatch.setenv("DECMUX_STATE_DIR", str(tmp_path / "state"))   # no store exists
    monkeypatch.setattr(cmux, "run_json",
                        lambda *a: {"caller": {"workspace_id": "ws-none", "surface_id": "s1"}})
    assert hooks.session_start() == 0
    assert json.loads(capsys.readouterr().out) == {}   # nothing injected


def test_injects_for_registered_manager(sandbox, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("DECMUX_ROLE", raising=False)
    monkeypatch.setenv("DECMUX_STATE_DIR", str(tmp_path / "state"))
    s = Store("ws-m", root=tmp_path / "state")
    s.bind_manager(surface_uuid="mgr", surface_ref="surface:9", cwd="/x")
    s.commit()
    monkeypatch.setattr(cmux, "run_json",
                        lambda *a: {"caller": {"workspace_id": "ws-m", "surface_id": "mgr"}})
    assert hooks.session_start() == 0
    out = json.loads(capsys.readouterr().out)
    assert "additionalContext" in out["hookSpecificOutput"]


# --- decmux only deletes data ---

def test_purge_current_then_all(sandbox, monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setenv("DECMUX_STATE_DIR", str(state))
    Store("ws-a", root=state)
    Store("ws-b", root=state)
    monkeypatch.setattr(cli, "_caller", lambda: {"workspace_id": "ws-a"})

    cli.main(["purge"])                                  # current workspace only
    assert not (state / "ws-a").exists() and (state / "ws-b").exists()

    cli.main(["purge", "--all"])                         # everything
    assert not state.exists()

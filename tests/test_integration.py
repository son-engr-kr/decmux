"""Agent integration: cmux-send guard, skill, and the SessionStart hook."""

from __future__ import annotations

import json

import pytest

from decmux import assets, cmux, hooks


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Redirect every on-disk asset path into a tmp dir, with a fake cmux."""
    monkeypatch.setattr(cmux, "CMUX_BIN", "/usr/bin/cmux")
    monkeypatch.setattr(assets, "GUARD_DIR", tmp_path / "bin")
    monkeypatch.setattr(assets, "GUARD_CMUX", tmp_path / "bin" / "cmux")
    monkeypatch.setattr(assets, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(assets, "STAMP", tmp_path / "skills" / ".version")
    monkeypatch.setattr(hooks, "CLAUDE_SETTINGS", tmp_path / "settings.json")
    return tmp_path


def test_guard_blocks_raw_input(sandbox):
    assets._ensure_cmux_guard()
    shim = (sandbox / "bin" / "cmux").read_text()
    assert "send|send-key" in shim and "decmux guard" in shim
    assert (sandbox / "bin" / "cmux").stat().st_mode & 0o111   # executable


def test_guarded_command_structure(sandbox):
    cmd = assets.guarded_command(
        "claude --foo",
        env={"DECMUX_ROLE": "agent", "CMUX_SURFACE_REF": "surface:1"},
        cwd="/work")
    assert cmd.startswith("cd /work &&")
    assert "DECMUX_REAL_CMUX=" in cmd and "PATH=" in cmd
    assert "DECMUX_ROLE=agent" in cmd and "claude --foo" in cmd


def test_skill_install_idempotent(sandbox):
    assert assets.ensure() is True             # first write
    assert (sandbox / "skills" / "SKILL.md").exists()
    assert assets.ensure() is False            # version-stamped: no rewrite


def test_hooks_install_and_status(sandbox):
    res = hooks.install_all_hooks()
    assert res["session_hook"] is True
    st = hooks.claude_status()
    assert st["session_start_hook"] is True and st["skill"] is True
    assert hooks.install_all_hooks()["session_hook"] is False   # idempotent


def test_hooks_remove_legacy_prompt(sandbox):
    (sandbox / "settings.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [
        {"hooks": [{"command": "cd x && uv run python -m decmux.codex_hook prompt-submit",
                    "type": "command"}]}]}}))
    assert hooks.uninstall_prompt_hooks() is True
    data = json.loads((sandbox / "settings.json").read_text())
    assert "UserPromptSubmit" not in data.get("hooks", {})


def test_session_start_reloads_skill(sandbox, capsys):
    assert hooks.session_start() == 0
    assert json.loads(capsys.readouterr().out) == {"reloadSkills": True}


def test_remove_integration(sandbox):
    assets.ensure()
    hooks.install_all_hooks()
    assert (sandbox / "skills" / "SKILL.md").exists()
    out = assets.remove()
    assert out["skill"] and out["guard"]
    assert not (sandbox / "skills").exists() and not (sandbox / "bin").exists()
    h = hooks.remove_hooks()
    assert h["session_removed"] is True
    assert hooks.claude_status()["session_start_hook"] is False


def test_uninstall_keeps_data_then_purges(sandbox, monkeypatch, tmp_path):
    from decmux import cli
    from decmux.store import Store
    state = tmp_path / "state"
    monkeypatch.setenv("DECMUX_STATE_DIR", str(state))
    Store("ws-x").set_goal("keep me")                 # some data to protect
    assets.ensure()
    hooks.install_all_hooks()

    cli.main(["uninstall"])                            # default: keep data
    assert not (sandbox / "skills").exists()           # integration removed
    assert hooks.claude_status()["session_start_hook"] is False
    assert (state / "ws-x" / "store.db").exists()      # data kept

    cli.main(["uninstall", "--data"])                  # opt-in wipe
    assert not state.exists()


def test_setup_then_uninstall_roundtrip(sandbox, monkeypatch, tmp_path):
    from decmux import cli
    monkeypatch.setenv("DECMUX_STATE_DIR", str(tmp_path / "state"))
    cli.main(["setup"])
    assert (sandbox / "skills" / "SKILL.md").exists()
    assert hooks.claude_status()["session_start_hook"] is True
    cli.main(["uninstall"])
    assert not (sandbox / "skills").exists()
    assert hooks.claude_status()["session_start_hook"] is False


def test_setup_hint_does_not_write(sandbox, capsys):
    from decmux import cli
    cli._setup_hint()                                  # skill not installed
    assert "decmux setup" in capsys.readouterr().out
    assert not (sandbox / "skills").exists()           # hint never writes global config

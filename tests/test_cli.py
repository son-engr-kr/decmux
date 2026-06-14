"""CLI verbs operate on the caller's workspace store (cmux I/O monkeypatched)."""

from __future__ import annotations

import pytest

from decmux import bus, cli
from decmux.store import Store

CALLER = {"workspace_id": "ws-test", "workspace_ref": "workspace:0",
          "surface_id": "s1", "surface_ref": "surface:1", "surface_type": "terminal"}


@pytest.fixture(autouse=True)
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("DECMUX_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "_caller", lambda: CALLER)
    monkeypatch.setattr(bus, "_deliver", lambda *a: None)
    monkeypatch.setattr(bus, "_ws_ref", lambda store: "")
    monkeypatch.setattr(bus, "resolve_sender", lambda store: "you")
    return tmp_path


def store(tmp_path):
    return Store("ws-test", root=tmp_path)


def test_goal_verb_persists_and_briefs(wired):
    assert cli.main(["goal", "ship", "v1"]) == 0
    assert store(wired).get_goal() == "ship v1"


def test_task_add_and_list(wired, capsys):
    assert cli.main(["task", "add", "do", "the", "thing"]) == 0
    tasks = store(wired).list_tasks()
    assert len(tasks) == 1 and tasks[0]["body"] == "do the thing"
    capsys.readouterr()
    assert cli.main(["task", "list"]) == 0
    assert "do the thing" in capsys.readouterr().out


def test_task_done_closes(wired):
    cli.main(["task", "add", "fix bug"])
    tid = store(wired).list_tasks()[0]["id"]
    assert cli.main(["task", "done", str(tid), "fixed"]) == 0
    assert store(wired).get_task(tid)["status"] == "done"


def test_send_creates_triage_task(wired):
    # a human message to the manager lands as a triage task
    assert cli.main(["send", "please", "look", "at", "X"]) == 0
    tasks = store(wired).list_tasks()
    assert len(tasks) == 1 and tasks[0]["status"] == "triage"


def test_parser_defaults_to_run():
    args = cli.build_parser().parse_args([])
    assert args.func is cli.cmd_run

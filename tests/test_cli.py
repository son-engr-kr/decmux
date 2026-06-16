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
    monkeypatch.setattr(bus, "_deliver", lambda *a: True)
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


def test_answer_to_human_task_surfaces_to_repl(wired, monkeypatch):
    cli.main(["task", "add", "is the build green?"])          # author defaults to you
    tid = store(wired).list_tasks()[0]["id"]
    monkeypatch.setattr(bus, "resolve_sender", lambda s: "manager")   # manager answers
    cli.main(["task", "answer", str(tid), "yes, all green"])
    chats = store(wired).recent_chat(kind="chat")
    assert any(c["frm"] == "manager" and "yes, all green" in c["body"] for c in chats)


def test_task_show_prints_thread(wired, capsys):
    s = store(wired)
    tid = s.add_task(kind="command", body="build the parser", to_whom="worker")
    s.task_progress(tid, "started analysis", author="worker")
    s.commit()
    capsys.readouterr()
    assert cli.main(["task", "show", str(tid)]) == 0
    out = capsys.readouterr().out
    assert "build the parser" in out and "started analysis" in out and "timeline" in out


def test_report_surfaces_recent_messages(wired, capsys):
    s = store(wired)
    s.add_chat(frm="worker-7", dst="manager", body="parser refactored, tests pass", kind="report")
    s.commit()
    capsys.readouterr()
    assert cli.main(["report"]) == 0
    out = capsys.readouterr().out
    assert "recent messages" in out and "parser refactored" in out


def test_worker_task_done_digests_to_manager(wired, monkeypatch):
    s = store(wired)
    s.upsert_state(surface_uuid="m1", surface_ref="surface:9", title="manager", state="idle")
    s.bind_manager(surface_uuid="m1", surface_ref="surface:9", cwd="/x")
    tid = s.add_task(kind="command", body="fix bug", to_whom="worker")
    s.commit()
    monkeypatch.setattr(bus, "resolve_sender", lambda store: "worker-7")
    assert cli.main(["task", "done", str(tid), "patched", "and", "verified"]) == 0
    pend = store(wired).pending_outbox("m1")
    assert pend and pend[0]["digest"] == 1
    assert f"#{tid}" in pend[0]["body"] and "worker-7" in pend[0]["body"]


def test_parser_defaults_to_app():
    args = cli.build_parser().parse_args([])
    assert args.func is cli.cmd_app   # no-arg `decmux` opens the interactive REPL


def test_agent_launch_plan():
    caller = {"workspace_id": "w", "workspace_ref": "workspace:1",
              "surface_id": "s", "surface_ref": "surface:1"}
    argv, env = cli._agent_launch(caller=caller, role="agent", kind="claude",
                                  command=None, guard_dir="/g", real_cmux="/r/cmux")
    assert argv[0] == "claude" and "--dangerously-skip-permissions" in argv
    assert env["DECMUX_ROLE"] == "agent" and env["CMUX_SURFACE_ID"] == "s"
    assert env["PATH"].startswith("/g:") and env["DECMUX_REAL_CMUX"] == "/r/cmux"


def test_update_check_reports_when_newer(wired, monkeypatch, capsys):
    from decmux import update
    monkeypatch.setattr(update, "latest_version", lambda timeout=3.0: "99.9.9")
    monkeypatch.setattr(update, "is_editable", lambda: False)
    assert cli.main(["update", "--check"]) == 0
    assert "update available" in capsys.readouterr().out


def test_update_up_to_date(wired, monkeypatch, capsys):
    from decmux import update
    monkeypatch.setattr(update, "latest_version", lambda timeout=3.0: "0.0.1")  # older
    assert cli.main(["update"]) == 0
    assert "up to date" in capsys.readouterr().out

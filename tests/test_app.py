"""REPL command handling (the prompt_toolkit loop is thin glue, not unit-tested)."""

from __future__ import annotations

import pytest

from decmux import app, bus
from decmux import session as session_mod
from decmux.store import Store


@pytest.fixture
def st(tmp_path, monkeypatch):
    monkeypatch.setattr(bus, "_deliver", lambda *a: None)
    monkeypatch.setattr(bus, "_ws_ref", lambda store: "")
    return app.AppState(Store("ws-test", root=tmp_path))


def test_plain_line_sends_to_target_and_tracks(st, capsys):
    assert app._handle(st, "please do x") is True
    assert "-> manager" in capsys.readouterr().out
    assert any(t["body"] == "please do x" for t in st.store.list_tasks())


def test_slash_to_changes_target(st):
    assert app._handle(st, "/to worker") is True
    assert st.target == "worker"


def test_slash_quit_returns_false(st):
    assert app._handle(st, "/quit") is False


def test_slash_goal_persists(st):
    assert app._handle(st, "/goal ship v1") is True
    assert st.store.get_goal() == "ship v1"


def test_known_command_without_arg_shows_usage(st, capsys):
    # `/goal` and `/to` are known: missing arg -> usage, not "unknown command"
    assert app._handle(st, "/goal") is True
    out = capsys.readouterr().out
    assert "usage: /goal" in out and "unknown" not in out
    assert st.store.get_goal() == ""          # nothing set
    assert app._handle(st, "/to") is True
    assert "usage: /to" in capsys.readouterr().out


def test_truly_unknown_command(st, capsys):
    assert app._handle(st, "/bogus") is True
    assert "unknown command: /bogus" in capsys.readouterr().out


def test_startup_guide_when_cold(st, capsys):
    app._startup_guide(st.store)                       # no manager, no managed agents
    assert "decmux agent --manager" in capsys.readouterr().out


def test_startup_guide_silent_with_team(st, capsys):
    st.store.mark_managed("u1")
    app._startup_guide(st.store)
    assert capsys.readouterr().out == ""               # team exists -> no guide


def test_slash_spawn_routes_to_spawn_agent(st, monkeypatch):
    calls = []
    monkeypatch.setattr(
        bus, "spawn_agent",
        lambda store, **k: calls.append(k) or
        {"created": True, "name": k.get("name") or "agent", "surface_ref": "surface:9",
         "manager": k.get("manager", False)})
    app._handle(st, "/spawn worker1")
    app._handle(st, "/spawn-manager")
    assert calls[0] == {"name": "worker1", "manager": False}
    assert calls[1] == {"name": None, "manager": True}


def test_blank_line_is_noop(st):
    assert app._handle(st, "   ") is True


def test_tasks_split_open_closed(st, capsys):
    st.store.add_task(kind="command", body="open one", to_whom="manager")
    done = st.store.add_task(kind="command", body="done one", to_whom="manager")
    st.store.close_task(done, "finished", "done")
    app._tasks(st.store, closed=False)
    out = capsys.readouterr().out
    assert "open one" in out and "done one" not in out
    app._tasks(st.store, closed=True)
    out = capsys.readouterr().out
    assert "done one" in out and "finished" in out


def test_task_detail_shows_timeline(st, capsys):
    tid = st.store.add_task(kind="command", body="the work", to_whom="worker")
    st.store.task_progress(tid, "started analysis", author="worker")
    app._task_detail(st.store, tid)
    out = capsys.readouterr().out
    assert "the work" in out and "started analysis" in out and "timeline" in out


def test_handle_task_and_tasks_closed(st):
    tid = st.store.add_task(kind="command", body="x")
    assert app._handle(st, f"/task {tid}") is True
    assert app._handle(st, "/task") is True            # usage, no crash
    assert app._handle(st, "/tasks closed") is True


def test_toolbar_renders(st):
    st.store.upsert_state(surface_uuid="a", surface_ref="surface:1", title="w", state="idle")
    bar = app._toolbar(st)
    assert "decmux" in bar and "->manager" in bar


def test_completions_carry_descriptions(st):
    st.store.upsert_state(surface_uuid="a", surface_ref="surface:1", title="worker", state="idle")
    words, meta = app._completions(st.store)
    assert "/status" in words and meta["/status"]          # command has a description
    assert "worker" in words and meta["worker"] == "agent"  # live agent name + meta
    assert meta["manager"] and meta["you"]                  # targets described


def test_repl_end_to_end_quit(tmp_path, monkeypatch):
    """Drive the real prompt_toolkit loop over a pipe (no cmux, no real tty)."""
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    monkeypatch.setenv("DECMUX_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(session_mod.Session, "run", lambda self, *a, **k: None)  # no cmux
    monkeypatch.setattr(bus, "_deliver", lambda *a: None)
    monkeypatch.setattr(bus, "_ws_ref", lambda store: "")

    with create_pipe_input() as pipe:
        pipe.send_text("hello manager\n/quit\n")
        with create_app_session(input=pipe, output=DummyOutput()):
            assert app.repl("ws-test", notify=False) == 0

    tasks = Store("ws-test", root=tmp_path).list_tasks()
    assert any(t["body"] == "hello manager" for t in tasks)

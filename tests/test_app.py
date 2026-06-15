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


def test_blank_line_is_noop(st):
    assert app._handle(st, "   ") is True


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

"""REPL smoke test (Session.run + cmux I/O stubbed)."""

from __future__ import annotations

import builtins

from decmux import app, bus
from decmux import session as session_mod


def test_repl_sends_then_quits(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DECMUX_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(session_mod.Session, "run", lambda self, *a, **k: None)
    monkeypatch.setattr(bus, "_deliver", lambda *a: None)
    monkeypatch.setattr(bus, "_ws_ref", lambda store: "")
    lines = iter(["please do x", "/tasks", "/quit"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(lines))

    assert app.repl("ws-test", notify=False) == 0
    out = capsys.readouterr().out
    assert "-> manager" in out          # the plain line was routed to the manager
    assert "please do x" in out         # it became a triage task, shown by /tasks

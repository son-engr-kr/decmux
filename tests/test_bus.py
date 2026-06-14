"""Routing / delivery invariants. cmux I/O (_deliver, _ws_ref) is monkeypatched;
the message logic and the store are exercised for real."""

from __future__ import annotations

import time

import pytest

from decmux import bus
from decmux.store import Store


@pytest.fixture
def s(tmp_path):
    return Store("ws-test", root=tmp_path)


@pytest.fixture
def recorder(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(bus, "_deliver", lambda sref, ws_ref, text: calls.append((sref, text)))
    monkeypatch.setattr(bus, "_ws_ref", lambda store: "")
    return calls


def add_agent(s, *, uuid, ref, name, state="idle", idle_secs=100):
    s.upsert_state(surface_uuid=uuid, surface_ref=ref, title=name, state=state,
                   last_active=time.time() - idle_secs)


# --- pure classifiers ---

def test_status_only_classifier():
    # status with no command signal -> withhold
    assert bus._looks_like_status_only("working on the parser, fyi") is True
    assert bus._looks_like_status_only("현재 진행 중입니다") is True
    # any command signal wins -> deliver
    assert bus._looks_like_status_only("fix the bug") is False
    assert bus._looks_like_status_only("로그 확인해줘") is False
    assert bus._looks_like_status_only("what about this?") is False
    assert bus._looks_like_status_only("see task #3") is False
    assert bus._looks_like_status_only("") is False


def test_is_downward():
    assert bus._is_downward("manager", "worker") is True
    assert bus._is_downward("manager", "you") is False
    assert bus._is_downward("manager", "manager") is False
    assert bus._is_downward("worker", "manager") is False


def test_gate_human_message():
    # a subordinate aimed at the human is rerouted to the manager
    to, text, gated = bus._gate_human_message("worker", "you", "hi human")
    assert to == "manager" and gated is True and "human-gate" in text
    # the manager (and the human) may reach the human directly
    assert bus._gate_human_message("manager", "you", "x") == ("you", "x", False)


# --- auto-close safety net ---

def test_auto_close_single_candidate(s):
    tid = s.add_task(kind="command", body="do x", to_whom="worker")
    closed = bus._auto_close_from_report(s, text="[AGENT-DONE] finished", frm="worker")
    assert closed == tid
    assert s.get_task(tid)["status"] == "done"


def test_auto_close_requires_marker_and_nonhuman(s):
    s.add_task(kind="command", body="do x", to_whom="worker")
    assert bus._auto_close_from_report(s, text="all good", frm="worker") is None      # no marker
    assert bus._auto_close_from_report(s, text="[AGENT-DONE]", frm="you") is None      # from human


def test_auto_close_explicit_id_not_reclosed(s):
    tid = s.add_task(kind="command", body="do x", to_whom="worker")
    assert bus._auto_close_from_report(s, text=f"[AGENT-DONE task #{tid}]", frm="worker") == tid
    # already closed -> no-op
    assert bus._auto_close_from_report(s, text=f"task #{tid} done", frm="worker") is None


def test_auto_close_ambiguous_no_op(s):
    s.add_task(kind="command", body="a", to_whom="worker")
    s.add_task(kind="command", body="b", to_whom="worker")
    # two open candidates, no explicit id -> refuse to guess
    assert bus._auto_close_from_report(s, text="[AGENT-DONE] done", frm="worker") is None


# --- send: idle-gated delivery ---

def test_send_delivers_to_idle(s, recorder):
    add_agent(s, uuid="m1", ref="surface:9", name="manager", state="idle")
    s.bind_manager(surface_uuid="m1", surface_ref="surface:9", cwd="/x")
    res = bus.send(s, "please run tests", to="manager", frm="you")
    assert res["delivered"] == 1 and res["queued"] == 0
    assert res["task"] is not None                 # human message -> triage task
    assert recorder and recorder[0][0] == "surface:9"


def test_send_queues_to_working(s, recorder):
    add_agent(s, uuid="m1", ref="surface:9", name="manager", state="working")
    s.bind_manager(surface_uuid="m1", surface_ref="surface:9", cwd="/x")
    res = bus.send(s, "please run tests", to="manager", frm="you")
    assert res["delivered"] == 0 and res["queued"] == 1
    assert recorder == []                            # nothing typed into a working surface
    assert s.pending_outbox("m1")                    # parked in the outbox


# --- send: downward status withholding ---

def test_send_withholds_downward_status(s, recorder):
    add_agent(s, uuid="w1", ref="surface:1", name="worker", state="idle")
    res = bus.send(s, "working on it, fyi", to="worker", frm="manager")
    assert res.get("withheld_status") is True
    assert res["delivered"] == 0 and recorder == []
    # a real command from the manager is delivered
    res2 = bus.send(s, "fix the parser", to="worker", frm="manager")
    assert res2["delivered"] == 1


def test_send_force_overrides_withhold(s, recorder):
    add_agent(s, uuid="w1", ref="surface:1", name="worker", state="idle")
    res = bus.send(s, "status update", to="worker", frm="manager", force=True)
    assert res["delivered"] == 1


# --- send: human-gate reroute ---

def test_send_human_gate_reroutes_to_manager(s, recorder):
    add_agent(s, uuid="m1", ref="surface:9", name="manager", state="idle")
    s.bind_manager(surface_uuid="m1", surface_ref="surface:9", cwd="/x")
    res = bus.send(s, "hey human", to="you", frm="worker")
    assert res["gated_to_manager"] is True
    assert res["delivered"] == 1                      # reached the manager, not the human
    assert recorder and "human-gate" in recorder[0][1]

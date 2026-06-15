"""Routing / delivery invariants. cmux I/O (_deliver, _ws_ref) is monkeypatched;
the message logic and the store are exercised for real."""

from __future__ import annotations

import time

import pytest

from decmux import assets, bus
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

@pytest.fixture
def fake_cmux(monkeypatch, tmp_path):
    """Records cmux.run calls; new-pane -> surface:5, new-surface -> surface:6."""
    from decmux import assets, cmux
    monkeypatch.setattr(cmux, "CMUX_BIN", "/usr/bin/cmux")
    monkeypatch.setattr(assets, "GUARD_DIR", tmp_path / "bin")
    monkeypatch.setattr(assets, "GUARD_CMUX", tmp_path / "bin" / "cmux")
    calls = []

    def run(*a):
        calls.append(a)
        if a and a[0] == "new-pane":
            return "OK surface:5 pane:5 workspace:1\n"
        if a and a[0] == "new-surface":
            return "surface:6 (DEADBEEF-0006)\n"     # uuid must be hex for the parser
        return ""

    def run_json(*a):
        if a and a[0] == "identify":
            ref = a[a.index("--surface") + 1]
            n = ref.rsplit(":", 1)[-1]
            return {"caller": {"surface_ref": ref, "surface_id": f"UUID-{n}",
                               "pane_ref": f"pane:{n}", "window_ref": "window:1"}}
        return {"workspaces": [{"id": "ws-test", "ref": "workspace:1",
                                "current_directory": "/x"}]}
    monkeypatch.setattr(cmux, "run", run)
    monkeypatch.setattr(cmux, "run_json", run_json)
    return calls


def test_spawn_manager_splits_and_binds(s, fake_cmux):
    res = bus.spawn_agent(s, manager=True)
    assert res["manager"] and s.manager()[0] == "UUID-5" and s.is_managed("UUID-5")
    assert any(c[0] == "new-pane" for c in fake_cmux)               # manager: own split pane
    assert bus.spawn_agent(s, manager=True)["created"] is False      # idempotent


def test_spawn_worker_without_manager_splits(s, fake_cmux):
    res = bus.spawn_agent(s, name="w1", manager=False)
    assert res["surface_ref"] == "surface:5" and s.is_managed("UUID-5")
    assert any(c[0] == "new-pane" for c in fake_cmux) and not s.manager()


def test_spawn_worker_joins_manager_pane_as_tab(s, fake_cmux):
    s.bind_manager(surface_uuid="UUID-5", surface_ref="surface:5", cwd="")
    s.mark_managed("UUID-5", "manager")
    s.commit()
    res = bus.spawn_agent(s, manager=False)
    assert res["surface_ref"] == "surface:6" and s.is_managed("DEADBEEF-0006")
    assert any(c[0] == "new-surface" and "--pane" in c for c in fake_cmux)  # joined as a tab
    assert not any(c[0] == "new-pane" for c in fake_cmux)                    # did not split


def test_spawn_default_name_has_surface_number(s, fake_cmux):
    assert bus.spawn_agent(s, manager=True)["name"] == "manager-5"


def test_deliver_protocol_queues(s):
    # onboarding a codex agent queues the full protocol once (de-mixed)
    oid = bus.deliver_protocol(s, "u1", "surface:1")
    assert oid > 0
    pending = s.pending_outbox("u1")
    assert pending and pending[0]["body"] == assets.PROTOCOL


def test_continue_thread_rebriefs_manager(s, recorder):
    tid = s.add_task(kind="command", body="fix login bug", to_whom="manager")
    s.task_progress(tid, "found token expiry", author="manager")
    add_agent(s, uuid="m1", ref="surface:9", name="manager", state="idle")
    s.bind_manager(surface_uuid="m1", surface_ref="surface:9", cwd="/x")
    bus.continue_thread(s, tid, "any update?", frm="human")
    assert any(c["body"] == "any update?" for c in s.task_comments(tid))   # follow-up recorded
    msg = recorder[-1][1]                                                   # delivered to manager
    assert "fix login bug" in msg and "found token expiry" in msg          # thread re-brief inline


def test_send_human_gate_reroutes_to_manager(s, recorder):
    add_agent(s, uuid="m1", ref="surface:9", name="manager", state="idle")
    s.bind_manager(surface_uuid="m1", surface_ref="surface:9", cwd="/x")
    res = bus.send(s, "hey human", to="you", frm="worker")
    assert res["gated_to_manager"] is True
    assert res["delivered"] == 1                      # reached the manager, not the human
    assert recorder and "human-gate" in recorder[0][1]

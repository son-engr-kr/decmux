"""Stuck-handling: the control plane pokes the manager, then escalates."""

from __future__ import annotations

import time

import pytest

from decmux import bus, cmux, session, watch
from decmux.config import WorkspaceConfig
from decmux.store import Store


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A session whose cmux I/O is captured, not executed."""
    delivers: list[tuple[str, str]] = []
    notifies: list[tuple] = []
    monkeypatch.setattr(bus, "_deliver", lambda sref, ws_ref, text: delivers.append((sref, text)))
    monkeypatch.setattr(bus, "_ws_ref", lambda store: "")
    monkeypatch.setattr(cmux, "run", lambda *a: notifies.append(a))
    store = Store("ws-test", root=tmp_path)
    sess = session.Session("ws-test", store=store, notify=True)
    sess.cfg = WorkspaceConfig(stuck_poke_after=10.0, escalation_timeout=20.0)
    return sess, store, delivers, notifies


def add_manager(store):
    store.upsert_state(surface_uuid="m1", surface_ref="surface:9", title="manager",
                       state="idle", last_active=time.time() - 1000)
    store.bind_manager(surface_uuid="m1", surface_ref="surface:9", cwd="/x")


def test_pokes_manager_once_then_escalates_then_rearms(wired):
    sess, store, delivers, notifies = wired
    add_manager(store)

    sess._health_step("a1", "stuck", "worker", now=0)      # just entered: nothing yet
    assert delivers == [] and notifies == []

    sess._health_step("a1", "stuck", "worker", now=15)     # past stuck_poke_after -> poke manager
    assert len(delivers) == 1
    assert delivers[0][0] == "surface:9" and "intervene" in delivers[0][1]

    sess._health_step("a1", "stuck", "worker", now=20)     # already poked, before escalate window
    assert len(delivers) == 1 and notifies == []

    sess._health_step("a1", "stuck", "worker", now=40)     # manager silent past escalation -> human
    assert len(notifies) == 1 and notifies[0][0] == "notify"

    # recovery clears the episode; a later stuck re-arms (pokes again)
    sess._health_step("a1", "idle", "worker", now=50)
    sess._health_step("a1", "stuck", "worker", now=61)
    assert len(delivers) == 1                               # within new stuck_poke_after window
    sess._health_step("a1", "stuck", "worker", now=75)
    assert len(delivers) == 2                               # re-armed and poked again


def test_no_manager_escalates_to_human(wired):
    sess, store, delivers, notifies = wired
    # no manager bound
    sess._health_step("a1", "dead", "worker", now=0)
    sess._health_step("a1", "dead", "worker", now=15)
    assert delivers == []                                   # nothing to poke
    assert len(notifies) == 1 and notifies[0][0] == "notify"


def test_non_alert_state_is_noop(wired):
    sess, store, delivers, notifies = wired
    add_manager(store)
    sess._health_step("a1", "working", "worker", now=100)
    sess._health_step("a1", "idle", "worker", now=200)
    assert delivers == [] and notifies == []


def test_manager_itself_escalates_to_human_not_self_poke(wired):
    sess, store, delivers, notifies = wired
    add_manager(store)
    sess._health_step("m1", "stuck", "manager", now=0, is_manager=True)
    sess._health_step("m1", "stuck", "manager", now=15, is_manager=True)
    assert delivers == []                 # never poke the manager about itself (no loop)
    assert len(notifies) == 1             # escalated to the human instead


def test_idle_with_no_work_is_not_stuck(wired):
    sess, store, delivers, notifies = wired
    add_manager(store)
    sess._health_step("a1", "stuck", "worker", now=0, has_work=False)
    sess._health_step("a1", "stuck", "worker", now=999, has_work=False)
    assert delivers == [] and notifies == []   # nothing assigned -> waiting, not stuck


def _row(uuid, ref, state="idle"):
    s = watch.Surface(ref=ref, uuid=uuid, pane="p", workspace="w", workspace_uuid="ws",
                      title="t", cpu=0.0, mem=0, procs=1)
    return watch.Row(surface=s, state=state, ws_name="w", ws_agent_tag=None, quiet_for=1.0)


def test_tick_only_manages_onboarded(tmp_path, monkeypatch):
    """B-scope: tick supervises only surfaces in the managed registry."""
    monkeypatch.setattr(bus, "_ws_ref", lambda store: "")
    store = Store("ws", root=tmp_path)
    store.mark_managed("u-managed")
    store.commit()
    sess = session.Session("ws", store=store, notify=False)
    monkeypatch.setattr(sess.watcher, "poll",
                        lambda *a, **k: [_row("u-managed", "surface:1"),
                                         _row("u-bare", "surface:2")])
    sess.tick(now=100.0)
    assert {a["surface_uuid"] for a in store.list_agents()} == {"u-managed"}


def test_tick_unmanages_closed_manager(tmp_path, monkeypatch):
    """A managed surface gone from cmux is unmanaged; a closed manager is unbound."""
    monkeypatch.setattr(bus, "_ws_ref", lambda store: "")
    store = Store("ws", root=tmp_path)
    store.mark_managed("u-mgr", "manager")
    store.bind_manager(surface_uuid="u-mgr", surface_ref="surface:1", cwd="")
    store.commit()
    sess = session.Session("ws", store=store, notify=False)

    def gone_poll(*a, **k):
        sess.watcher.present_surfaces = set()          # surface no longer in cmux
        return []
    monkeypatch.setattr(sess.watcher, "poll", gone_poll)
    sess.tick(now=100.0)
    assert store.manager() is None                      # binding cleared
    assert store.managed_set() == set()                 # unmanaged


def test_tick_keeps_present_but_unclassified_surface(tmp_path, monkeypatch):
    """A managed surface that exists but isn't an agent yet (just spawned) is kept."""
    monkeypatch.setattr(bus, "_ws_ref", lambda store: "")
    store = Store("ws", root=tmp_path)
    store.mark_managed("u1")
    store.commit()
    sess = session.Session("ws", store=store, notify=False)

    def booting_poll(*a, **k):
        sess.watcher.present_surfaces = {"u1"}          # exists, not classified yet
        return []
    monkeypatch.setattr(sess.watcher, "poll", booting_poll)
    sess.tick(now=100.0)
    assert store.managed_set() == {"u1"}                # present -> not unmanaged


# --- workforce reaper + proactive momentum ---

from types import SimpleNamespace


def _lrow(key, ref, title, state):
    return SimpleNamespace(surface=SimpleNamespace(key=key, ref=ref, title=title), state=state)


def test_reaps_self_agent_when_idle_and_done(wired, monkeypatch):
    sess, store, delivers, notifies = wired
    monkeypatch.setattr(cmux, "read_screen", lambda *a, **k: "transcript")
    store.mark_managed("w1", role="agent", term="short", origin="self")
    sess.cfg = WorkspaceConfig(reap_short_grace=10.0)
    row = _lrow("w1", "surface:3", "w1", "idle")
    sess._reap_step(row, now=0)                         # idle clock starts
    assert store.is_managed("w1")
    sess._reap_step(row, now=20)                        # past grace -> auto-reaped
    assert not store.is_managed("w1")
    assert any(a[0] == "close-surface" for a in notifies)


def test_human_agent_not_auto_reaped_only_asked(wired, monkeypatch):
    sess, store, delivers, notifies = wired
    monkeypatch.setattr(cmux, "read_screen", lambda *a, **k: "x")
    store.mark_managed("h1", role="agent", term="short", origin="human")
    sess.cfg = WorkspaceConfig(reap_short_grace=10.0)
    row = _lrow("h1", "surface:4", "h1", "idle")
    sess._reap_step(row, now=0)
    sess._reap_step(row, now=20)
    assert store.is_managed("h1")                       # never auto-closed
    assert not any(a[0] == "close-surface" for a in notifies)
    assert any(a[0] == "notify" for a in notifies)      # human asked once


def test_reaper_keeps_agent_with_open_task(wired, monkeypatch):
    sess, store, delivers, notifies = wired
    monkeypatch.setattr(cmux, "read_screen", lambda *a, **k: "x")
    store.mark_managed("w2", term="short", origin="self")
    store.add_task(kind="command", body="do it", to_whom="w2")
    sess.cfg = WorkspaceConfig(reap_short_grace=1.0)
    row = _lrow("w2", "surface:5", "w2", "idle")
    sess._reap_step(row, now=100)                       # has open assigned work -> kept
    assert store.is_managed("w2")
    assert not any(a[0] == "close-surface" for a in notifies)


def test_manager_never_reaped(wired, monkeypatch):
    sess, store, delivers, notifies = wired
    monkeypatch.setattr(cmux, "read_screen", lambda *a, **k: "x")
    add_manager(store)
    store.mark_managed("m1", role="manager", term="full", origin="self")
    sess.cfg = WorkspaceConfig(reap_short_grace=1.0)
    row = _lrow("m1", "surface:9", "manager", "idle")
    sess._reap_step(row, now=100)
    assert store.is_managed("m1")


def test_momentum_nudges_once_then_waits(wired):
    sess, store, delivers, notifies = wired
    add_manager(store)
    store.set_goal("ship v1")
    sess.cfg = WorkspaceConfig(momentum=True, momentum_cooldown=300.0)
    rows = [_lrow("m1", "surface:9", "manager", "idle")]
    sess._momentum_step(now=1000, rows=rows)
    assert len(delivers) == 1 and "Advance it" in delivers[0][1]
    sess._momentum_step(now=1100, rows=rows)            # still coasting, armed -> no repeat
    assert len(delivers) == 1
    sess._momentum_step(now=1100, rows=[_lrow("m1", "surface:9", "manager", "working")])  # busy: re-arm
    sess._momentum_step(now=1200, rows=rows)            # idle again but within cooldown
    assert len(delivers) == 1
    sess._momentum_step(now=1400, rows=rows)            # past cooldown + coasting -> one more
    assert len(delivers) == 2


def test_momentum_silent_without_goal_or_with_open_tasks(wired):
    sess, store, delivers, notifies = wired
    add_manager(store)
    sess.cfg = WorkspaceConfig(momentum=True)
    rows = [_lrow("m1", "surface:9", "manager", "idle")]
    sess._momentum_step(now=0, rows=rows)               # no goal -> silent
    assert delivers == []
    store.set_goal("ship")
    store.add_task(kind="command", body="x", to_whom="manager")
    sess._momentum_step(now=0, rows=rows)               # open task -> the pulse's job, not momentum
    assert delivers == []


def test_next_wakeup_reports_soonest_open_task(wired):
    sess, store, delivers, notifies = wired
    sess.cfg = WorkspaceConfig(momentum=True, momentum_cooldown=300.0)
    tid = store.add_task(kind="command", body="x", to_whom="manager", now=1000.0)
    rows = [_lrow("m1", "surface:9", "manager", "idle")]
    ts, label = sess._next_wakeup(now=1000.0, rows=rows)
    assert label == f"task #{tid} review"
    assert abs(ts - (1000.0 + session.TASK_REVIEW_AFTER)) < 1
    sess._persist_next_wakeup(now=1000.0, rows=rows)
    assert store.get_meta("next_wakeup_kind") == f"task #{tid} review"
    assert store.get_meta("next_wakeup_ts")


def test_next_wakeup_none_when_idle_and_empty(wired):
    sess, store, delivers, notifies = wired
    ts, label = sess._next_wakeup(now=0.0, rows=[])
    assert ts is None and label == ""
    sess._persist_next_wakeup(now=0.0, rows=[])
    assert store.get_meta("next_wakeup_ts") == ""

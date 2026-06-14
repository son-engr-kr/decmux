"""Stuck-handling: the control plane pokes the manager, then escalates."""

from __future__ import annotations

import time

import pytest

from decmux import bus, cmux, session
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

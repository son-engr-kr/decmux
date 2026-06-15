"""Store invariants that must survive the rewrite (single-workspace)."""

from __future__ import annotations

import pytest

from decmux.store import Store


def mk(tmp_path) -> Store:
    return Store("ws-test", root=tmp_path)


def test_task_source_id_idempotent(tmp_path):
    s = mk(tmp_path)
    a = s.add_task(kind="command", body="do x", source_id="abc")
    b = s.add_task(kind="command", body="do x again", source_id="abc")
    assert a == b  # same source_id -> same task, no duplicate
    assert len(s.list_tasks()) == 1


def test_outbox_per_task_dedup(tmp_path):
    s = mk(tmp_path)
    first = s.enqueue_outbox(surface_uuid="u1", surface_ref="surface:1", body="m", task_id=5)
    dup = s.enqueue_outbox(surface_uuid="u1", surface_ref="surface:1", body="m", task_id=5)
    assert first > 0
    assert dup == 0  # already a pending copy for (surface, task)
    # a different surface is not deduped
    other = s.enqueue_outbox(surface_uuid="u2", surface_ref="surface:2", body="m", task_id=5)
    assert other > 0


def test_held_counted_but_not_flushed(tmp_path):
    s = mk(tmp_path)
    oid = s.enqueue_outbox(surface_uuid="u1", surface_ref="surface:1", body="m", task_id=7)
    s.update_outbox(oid, status="held")
    # held is still a pending copy for dedup...
    assert s.has_pending_outbox(surface_uuid="u1", task_id=7) is True
    # ...but never appears in the flush list (strictly 'pending')
    assert s.pending_outbox("u1") == []


def test_update_outbox_rejects_delivered(tmp_path):
    s = mk(tmp_path)
    oid = s.enqueue_outbox(surface_uuid="u1", surface_ref="surface:1", body="m")
    s.mark_outbox_delivered([oid])
    assert s.pending_outbox("u1") == []
    with pytest.raises(AssertionError):
        s.update_outbox(oid, status="held")  # compare-and-set guard


def test_close_then_reopen_task(tmp_path):
    s = mk(tmp_path)
    tid = s.add_task(kind="command", body="work")
    s.close_task(tid, "done it", status="done")
    assert s.get_task(tid)["status"] == "done"
    assert s.get_task(tid)["closed_at"] is not None
    assert s.open_tasks() == []  # closed tasks are not open
    s.reopen_task(tid)
    assert s.get_task(tid)["status"] == "open"
    assert s.get_task(tid)["closed_at"] is None
    assert len(s.open_tasks()) == 1


def test_reassign_manager_work(tmp_path):
    s = mk(tmp_path)
    s.bind_manager(surface_uuid="old", surface_ref="surface:1", cwd="/x")
    tid = s.add_task(kind="command", body="mtask", to_whom="manager")
    s.increment_task_delivered(tid)  # pretend delivered once
    s.enqueue_outbox(surface_uuid="old", surface_ref="surface:1", body="poke", task_id=tid)
    res = s.reassign_manager_work(
        old_surface_uuid="old", old_surface_ref="surface:1",
        new_surface_uuid="new", new_surface_ref="surface:2")
    assert res["moved_outbox"] == 1
    assert res["requeued_tasks"] == 1
    # task delivery clock cleared so the pulse redelivers
    assert s.get_task(tid)["delivered"] == 0
    # outbox now points at the new surface
    assert s.pending_outbox("new") and not s.pending_outbox("old")


def test_reconcile_prune(tmp_path):
    s = mk(tmp_path)
    s.upsert_state(surface_uuid="a", surface_ref="surface:1", title="A", state="idle")
    s.upsert_state(surface_uuid="b", surface_ref="surface:2", title="B", state="working")
    assert set(s.last_states()) == {"a", "b"}
    s.prune_absent({"a"})  # b is gone
    assert set(s.last_states()) == {"a"}


def test_model_effort_sticky(tmp_path):
    s = mk(tmp_path)
    s.upsert_state(surface_uuid="a", surface_ref="surface:1", title="A",
                   state="working", model="opus", effort="high")
    # a later poll with no model/effort keeps the last detected values
    s.upsert_state(surface_uuid="a", surface_ref="surface:1", title="A", state="idle")
    row = s.agent_by_uuid("a")
    assert row["model"] == "opus" and row["effort"] == "high"
    assert row["state"] == "idle"


def test_goal_roundtrip(tmp_path):
    s = mk(tmp_path)
    assert s.get_goal() == ""
    s.set_goal("ship v1")
    assert s.get_goal() == "ship v1"


def test_chat_and_transition_watermarks(tmp_path):
    s = mk(tmp_path)
    base_chat = s.last_chat_id()
    s.add_chat(frm="you", dst="manager", body="hi", kind="chat")
    s.add_chat(frm="manager", dst="you", body="ok", kind="chat")
    new = s.chat_after(base_chat, kind="chat")
    assert [c["body"] for c in new] == ["hi", "ok"]
    # watermark advances; nothing new after the last id
    assert s.chat_after(new[-1]["id"]) == []

    base_tr = s.last_transition_id()
    s.record_transition(surface_uuid="a", title="A", from_state="idle", to_state="stuck")
    tr = s.transitions_after(base_tr)
    assert len(tr) == 1 and tr[0]["to_state"] == "stuck"

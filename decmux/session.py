"""The per-workspace session: the foreground supervision loop.

While `decmux` is open this owns the store, the watchdog, and the message bus, and
on each tick it: classifies agents, persists state, flushes one queued message per
idle turn, runs the manager pulse (task reminders/escalation), routes Feed
decisions, and — the headline feature — handles stuck agents by poking the
manager, escalating to the human only if the manager does not act.

There is no background daemon: closing decmux stops this loop. Durable state lives
in the store, so the next launch reconciles (re-attach by surface UUID) and resumes.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections import Counter

from . import bus, cmux, policy, shell_state, watch
from . import config as config_mod
from .store import Store

ALERT_STATES = {"blocked-on-decision", "stuck", "error", "dead"}
# states where the manager should intervene (the control plane pokes it)
POKE_STATES = {"stuck", "error", "dead"}

DECISION_GRACE = 2.0
TASK_REVIEW_AFTER = 600.0
TASK_ESCALATE_AFTER = 1800.0

_PROC_NOISE = {"cmux", "caffeinate", "sleep", "login", "awk", "sed", "grep", "-zsh", "tail"}

# state -> (pill code, hex color) for the cmux-native status pill
PILL = {
    "working": ("W", "#2ecc71"), "idle": ("I", "#3498db"),
    "stuck": ("S", "#f1c40f"), "error": ("E", "#e74c3c"),
    "dead": ("D", "#e74c3c"), "budget": ("B", "#9b59b6"),
    "blocked-on-decision": ("?", "#f39c12"),
}
_WORST = ["error", "dead", "stuck", "blocked-on-decision", "budget", "idle", "working"]


def _top_procs(names) -> list[str]:
    """Distinct, interesting process names for an agent (drops shell/util noise)."""
    out: list[str] = []
    for n in names:
        if n and n not in _PROC_NOISE and n not in out:
            out.append(n)
    return out[:10]


class Session:
    def __init__(self, workspace_uuid: str, *, store: Store | None = None,
                 cfg: config_mod.Config | None = None, notify: bool = True,
                 pin: bool = False) -> None:
        self.workspace_uuid = workspace_uuid
        self.config = cfg or config_mod.load()
        self.cfg = self.config.defaults
        self.store = store or Store(workspace_uuid)
        self.watcher = watch.Watcher(workspace_uuid, self.config)
        self.shell_tracker = shell_state.ShellTracker()
        self.notify = notify
        self.pin = pin
        self.ws_ref = ""

        self.known = self.store.last_states()
        self.seeding = not self.known
        self.flushed_idle: set[str] = set()
        # per stuck-episode bookkeeping (in-memory, re-armed on recovery)
        self._stuck_since: dict[str, float] = {}
        self._poked: set[str] = set()
        self._escalated: set[str] = set()

        self._events_proc = None
        self._q: queue.Queue = queue.Queue()
        self.pending: dict[str, tuple] = {}
        self.alerted: set[str] = set()

    # --- alerts to the human (never typed into an agent surface) ---
    def _notify_human(self, title: str, body: str) -> None:
        if not self.notify:
            return
        args = ["notify", "--title", title, "--body", body]
        if self.ws_ref:
            args += ["--workspace", self.ws_ref]
        try:
            cmux.run(*args)
        except (subprocess.CalledProcessError, OSError):
            pass

    # --- stuck-handling: poke the manager once, then escalate to the human ---
    def _health_step(self, key: str, state: str, name: str, now: float, *,
                     is_manager: bool = False, has_work: bool = True) -> None:
        # An agent idle with nothing assigned isn't "stuck" — it's waiting (the
        # manager after clearing triage, or an unassigned worker). Only treat
        # `stuck` as actionable when there's work to stall on; error/dead always are.
        benign = state == "stuck" and not has_work
        if state not in POKE_STATES or benign:
            self._stuck_since.pop(key, None)
            self._poked.discard(key)
            self._escalated.discard(key)
            return
        self._stuck_since.setdefault(key, now)
        elapsed = now - self._stuck_since[key]
        if elapsed < self.cfg.stuck_poke_after:
            return
        mins = int(elapsed // 60)
        # the manager itself (or no manager bound) is the human's call — never poke
        # the manager about itself, which just loops.
        to_human = is_manager or not self.store.manager()
        if key not in self._poked:
            if to_human:
                self._notify_human(f"decmux: {name} {state}",
                                   f"{name} {state} {mins}m — needs you")
            else:
                bus.send(
                    self.store,
                    f"agent {name} {state} {mins}m — intervene (nudge / reassign / respawn).",
                    to="manager", frm="decmux", track_task=False,
                )
            self._poked.add(key)
            return
        # poked the manager about a worker: escalate to the human if it stays unresolved
        if (not to_human and key not in self._escalated
                and elapsed >= self.cfg.stuck_poke_after + self.cfg.escalation_timeout):
            self._notify_human(f"decmux: still {state} (manager silent)",
                               f"{name} {state} {mins}m")
            self._escalated.add(key)

    # --- manager pulse: keep open tasks from rotting ---
    def _manager_pulse(self, now: float) -> None:
        mgr = self.store.manager()
        for t in self.store.open_tasks():
            tid = t["id"]
            last = t.get("last_reminded_at") or t.get("delivered_at") or t.get("ts") or 0.0
            if now - last >= TASK_REVIEW_AFTER:
                if mgr:
                    if self.store.task_has_pending_delivery(tid):
                        self.store.mark_task_reminded(
                            tid, body="still open: delivery queued until the agent is idle", now=now)
                    elif not t.get("delivered") and bus.deliver_task(self.store, t):
                        self.store.mark_task_reminded(
                            tid, body="still open: delivered to manager", now=now)
                    else:
                        self.store.mark_task_reminded(
                            tid, body="still open: waiting for manager to delegate/answer/close",
                            now=now)
                else:
                    self._notify_human("decmux: no manager for open task",
                                       f"#{tid} {t['body'][:100]}")
                    self.store.mark_task_reminded(
                        tid, body="still open: no manager bound, escalated to human", now=now)
            age = now - (t.get("ts") or now)
            if age >= TASK_ESCALATE_AFTER and not t.get("escalated_at"):
                self.store.mark_task_escalated(
                    tid, body="open task exceeded manager response window", now=now)
                self._notify_human("decmux: task still open", f"#{tid} {t['body'][:100]}")

    def _pin(self, rows: list[watch.Row]) -> None:
        if not self.ws_ref:
            return
        counts = Counter(r.state for r in rows)
        summary = " ".join(f"{PILL.get(s, (s,))[0]}{n}" for s, n in counts.items())
        shell = sum(1 for r in rows if r.busy_kind == "shell")
        if shell:
            summary += f" ⚙{shell}"
        worst = next((s for s in _WORST if counts.get(s)), "working")
        try:
            cmux.run("set-status", "decmux", summary or "idle", "--workspace", self.ws_ref,
                     "--color", PILL.get(worst, ("?", "#888"))[1], "--priority", "5")
        except (subprocess.CalledProcessError, OSError):
            pass

    # --- event stream (push): liveness, shell-state, Feed decisions ---
    def start_events(self) -> None:
        cursor = self.store.dir / "events.cursor"
        self._events_proc = cmux.events_popen(
            categories=["feed", "agent"], cursor_file=str(cursor), reconnect=True)
        threading.Thread(target=self._consume, daemon=True).start()

    def _consume(self) -> None:
        assert self._events_proc and self._events_proc.stdout
        for line in self._events_proc.stdout:
            line = line.strip()
            if not line:
                continue
            frame = json.loads(line)
            if frame.get("type") == "event":
                self._q.put(frame)

    def _drain_events(self, now: float) -> None:
        while True:
            try:
                f = self._q.get_nowait()
            except queue.Empty:
                break
            if f.get("workspace_id") != self.workspace_uuid:
                continue   # other workspaces are not ours
            name = f.get("name", "")
            payload = f.get("payload") or {}
            self.watcher.note_activity(now)               # liveness
            self.shell_tracker.observe(name=name, payload=payload, now=now)
            self.store.log_event(kind=name, payload=json.dumps(payload)[:2000])
            rid = payload.get("_opencode_request_id") or payload.get("session_id")
            if name == "feed.item.received" and rid:
                self.pending[rid] = (now, payload.get("hook_event_name", ""),
                                     payload.get("tool_name", ""))
            elif name == "feed.item.completed" and rid:
                self.pending.pop(rid, None)
                self.alerted.discard(rid)
                self.store.resolve_decision(rid, "completed")

        for rid, (ts, hook, tool) in list(self.pending.items()):
            if now - ts >= DECISION_GRACE and rid not in self.alerted:
                self.alerted.add(rid)
                disposition = policy.decide(hook_event=hook, tool_name=tool)
                self.store.add_decision(request_id=rid, kind="feed", hook_event=hook,
                                        tool_name=tool, disposition=disposition)
                self.store.record_transition(surface_uuid=rid, title=hook,
                                             from_state=None, to_state="blocked-on-decision")
                if disposition == "auto" and self.cfg.auto_answer:
                    try:
                        cmux.feed_reply_permission(rid, "once")
                    except (subprocess.CalledProcessError, OSError):
                        pass
                else:
                    self._notify_human("decmux: needs input", f"{hook} {tool} awaiting")

    # --- one supervision tick ---
    def tick(self, now: float | None = None) -> list[watch.Row]:
        now = now if now is not None else time.time()
        if not self.ws_ref:
            self.ws_ref = bus._ws_ref(self.store)
        rows = self.watcher.poll(now, shell_ppids=self.shell_tracker.active_ppids(now))
        # B-scope: supervise only surfaces decmux onboarded (spawn/agent/register),
        # so a bare `claude` or your own driver session is never watched or poked.
        managed = self.store.managed_set()
        rows = [r for r in rows if r.surface.key in managed]
        if rows and rows[0].workspace_cwd:        # workspace dir, for `/status`
            self.store.set_meta("cwd", rows[0].workspace_cwd)
        mgr = self.store.manager()
        mgr_uuid = mgr[0] if mgr else None
        present: set[str] = set()
        for r in rows:
            key = r.surface.key
            present.add(key)
            prev = self.known.get(key)
            self.cfg = self.config.for_workspace(r.workspace_cwd, r.ws_name)
            if not self.seeding and r.state != prev and r.state in ALERT_STATES:
                self.store.record_transition(surface_uuid=key, title=r.surface.title,
                                             from_state=prev, to_state=r.state)
            if r.state == "budget" and prev != "budget":
                self._notify_human("decmux: usage limit",
                                   f"{bus._clean_name(r.surface.title)} hit a usage/rate limit")
            self.store.upsert_state(
                surface_uuid=key, surface_ref=r.surface.ref, title=r.surface.title,
                state=r.state, last_active=now - (r.quiet_for or 0),
                procs=json.dumps(_top_procs(r.surface.proc_names)),
                model=(r.model or None), effort=(r.effort or None),
                busy_kind=(r.busy_kind or None))
            is_mgr = key == mgr_uuid
            actor = "manager" if is_mgr else bus._clean_name(r.surface.title)
            has_work = bool(self.store.open_tasks_for_actor(actor=actor))
            self._health_step(key, r.state, bus._clean_name(r.surface.title), now,
                              is_manager=is_mgr, has_work=has_work)
            # one queued message per idle turn, re-armed when the agent leaves idle
            if r.state != "idle":
                self.flushed_idle.discard(key)
            elif key not in self.flushed_idle:
                if bus.flush_outbox(self.store, key, r.surface.ref, self.ws_ref):
                    self.flushed_idle.add(key)

        # A managed surface absent from cmux entirely has been closed: unmanage it,
        # and drop the manager binding if it was the manager — so decmux notices a
        # closed manager instead of routing to a dead surface forever.
        for uuid in managed - self.watcher.present_surfaces:
            was_manager = bool(self.store.manager()) and self.store.manager()[0] == uuid
            self.store.unmark_managed(uuid)
            self.store.record_transition(surface_uuid=uuid, title="(surface closed)",
                                         from_state=self.known.get(uuid), to_state="dead")
            if was_manager:
                self.store.clear_manager()
                self._notify_human("decmux: manager surface closed",
                                   "no manager bound — spawn one with /spawn-manager")

        self._manager_pulse(now)
        if self.pin:
            self._pin(rows)
        self.store.prune_absent(present)
        self._drain_events(now)
        self.store.commit()
        self.known = {r.surface.key: r.state for r in rows}
        self.seeding = False
        return rows

    def run(self, interval: float = 5.0, ticks: int = 0) -> int:
        self.start_events()
        n = 0
        try:
            while True:
                self.tick()
                n += 1
                if ticks and n >= ticks:
                    return 0
                time.sleep(interval)
        except KeyboardInterrupt:
            return 0
        finally:
            self.close()

    def close(self) -> None:
        if self._events_proc is not None:
            self._events_proc.terminate()
            self._events_proc = None

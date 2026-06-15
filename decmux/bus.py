"""Message hub: agents, the manager, and you route messages through decmux.

`send()` records the message in the chat log and either delivers it to the target
surface(s) via cmux or queues it while a recipient is working. The skill tells
agents to use `decmux send` instead of raw `cmux send`, so every message is logged
and de-mixed. One store = one workspace, so nothing here is workspace-scoped.
"""

from __future__ import annotations

import re
import subprocess
import time

from . import assets, cmux

_HUMAN = {"you", "user", "me", "human"}
_ALL = {"all", "everyone", "broadcast"}
BUSY_GRACE_SECONDS = 60.0

# Separator between a sender's ACTUAL message and decmux's appended system
# boilerplate/instructions. Long injected messages otherwise blur the two; this
# puts the sender's content first, then a rule + label before decmux's text.
_SYS_SEP = "\n\n---\n— decmux (system) —"

_DONE_MARKER = re.compile(
    r"(\[AGENT-DONE\b|\[TASK-DONE\b|\btask\s+#?\d+\s+"
    r"(done|closed|answered|complete|completed|fixed|implemented|verified)\b)",
    re.I,
)
_TASK_REF = re.compile(r"\b(?:task|triage)\s+#?(\d+)\b|#(\d+)\b", re.I)

# --- downward status guard (manager -> subordinate must be commands, not status) ---
# The manager protocol is commands DOWN, reports UP. A status-only message sent to
# a subordinate just interrupts its work, so decmux withholds it. Classification is
# intentionally regex/keyword-level (no NLP) and conservative: a command signal
# always wins (never block a directive), ambiguous text passes, and only clear
# status with no command signal is withheld. Bilingual (EN + KO) by design.
_CMD_SIGNAL = re.compile(
    r"\?\s*$"                                      # a question
    r"|#\d+|\btask\s+#?\d+\b"                      # task/triage reference
    r"|```|`|/\w"                                   # code fence, inline code, or path
    r"|^\s*(?:please|pls)\b"
    r"|^\s*(?:fix|add|implement|run|check|investigate|use|make|build|review|merge|"
    r"test|create|remove|update|refactor|delete|write|read|open|close|start|stop|"
    r"deploy|install|set|configure|rename|move|copy|fetch|pull|push|commit|rebase|"
    r"revert|ensure|verify|handle|apply|enable|disable|spawn|assign|delegate|reply|"
    r"send|do|don't|go|keep|hold|wait|drop|split|rebuild|retry|rerun|focus|finish)\b"
    r"|해줘|해 줘|해주세요|하세요|해라|하라|할 ?것|바람|부탁|확인해|수정해|구현해|"
    r"실행해|진행해|검토해|추가해|삭제해|만들어|고쳐|체크해|해야",
    re.I | re.M,
)
_STATUS_SIGNAL = re.compile(
    r"\b(?:status|update|fyi|heads[- ]?up|just so you know|for your awareness|"
    r"for awareness|progress|currently|so far|as of now|working on|i'?m\b|we'?re\b)"
    r"|\(delivered\s+\d+|queued\s+\d+\)"            # report-template echoes
    r"|현재|상황|보고|진행\s*(?:상황|중)|완료(?:했|됐|됨|되었|하였)|"
    r"했습니다|하고\s*있|중입니다|드립니다|상태",
    re.I,
)


def _looks_like_status_only(text: str) -> bool:
    """True only for clear status/report text carrying no command signal."""
    t = (text or "").strip()
    if not t or _CMD_SIGNAL.search(t):
        return False
    return bool(_STATUS_SIGNAL.search(t))


def _is_downward(frm: str, to: str) -> bool:
    """A manager message aimed at subordinate agent(s) — a named agent or broadcast."""
    return (frm.strip().lower() == "manager"
            and to.strip().lower() not in _HUMAN | {"manager"})


def _ws_refs() -> dict[str, str]:
    try:
        wl = cmux.run_json("workspace", "list", "--id-format", "both", "--json")
        return {w["id"]: w["ref"] for w in wl.get("workspaces", [])}
    except (subprocess.CalledProcessError, OSError, KeyError):
        return {}


def _ws_ref(store) -> str:
    """The cmux ref for this store's workspace (needed for send --workspace)."""
    return _ws_refs().get(store.workspace_uuid, "")


# Surface titles carry a leading status glyph (braille spinner ⠐, ✳/✻ markers)
# and are often a long task description — strip to a short, clean handle.
_GLYPHS = re.compile(r"^[\s✀-➿⠀-⣿*·•●○◐◑✦✧⋆]+")


def _clean_name(title: str) -> str:
    return _GLYPHS.sub("", title or "").strip()[:24] or "agent"


def _agents(store) -> list[tuple[str, str, str]]:
    """(surface_ref, surface_uuid, name) for every known agent in this workspace."""
    return [(a["surface_ref"], a["surface_uuid"], _clean_name(a["title"]))
            for a in store.list_agents()]


def resolve_sender(store) -> str:
    try:
        sid = cmux.run_json("identify", "--id-format", "both", "--json")["caller"].get("surface_id")
    except (subprocess.CalledProcessError, OSError, KeyError):
        return "you"
    if sid:
        if store.is_manager(sid):   # a bound manager speaks as "manager"
            return "manager"
        a = store.agent_by_uuid(sid)
        if a and a.get("title"):
            return _clean_name(a["title"])
    return "you"


def _targets(store, to: str) -> list[tuple[str, str, str]]:
    t = to.strip().lower()
    if t in _HUMAN:
        return []   # human messages never resolve to a surface (notify/chat only)
    agents = _agents(store)
    if t in _ALL:
        return agents
    if t == "manager":
        m = store.manager()
        return [a for a in agents if a[0] == m[1]] if m else []
    named = [a for a in agents if a[2].lower() == t]
    if named:
        return named
    if to.startswith("surface:"):
        return [a for a in agents if a[0] == to]
    return []


_SEND_SETTLE = 0.25   # seconds: let sent text land in the input before Enter
_SUBMIT_RETRIES = 2   # re-press Enter if the line is still sitting unsent


def _deliver(surface_ref: str, ws_ref: str, text: str) -> None:
    base = (["--workspace", ws_ref] if ws_ref else []) + ["--surface", surface_ref]
    cmux.run("send", *base, text)
    # Pressing Enter immediately races ahead of the text settling in the input,
    # leaving the line unsent. Settle first, press Enter, then verify the line
    # actually submitted and re-press if it's still sitting in the input.
    tail = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")[:48]
    for _ in range(_SUBMIT_RETRIES + 1):
        time.sleep(_SEND_SETTLE)
        cmux.run("send-key", *base, "Enter")
        if _line_submitted(surface_ref, ws_ref, tail):
            return


def _line_submitted(surface_ref: str, ws_ref: str, tail: str) -> bool:
    """Best-effort check that the input box no longer holds the just-sent line.
    A submitted message moves into the transcript, leaving the bottom input line
    clear; if the tail still occupies the last visible line, Enter didn't take.
    Returns True (assume submitted) when there's nothing to check or the read
    fails, so a read error never causes an Enter-spin."""
    if not tail:
        return True
    try:
        time.sleep(_SEND_SETTLE)
        screen = cmux.read_screen(surface_ref, workspace=ws_ref or None, lines=6)
    except (subprocess.CalledProcessError, OSError):
        return True
    lines = [ln for ln in screen.splitlines() if ln.strip()]
    return not lines or tail not in lines[-1]


def _should_queue(info: dict | None, now: float | None = None,
                  recent_grace: bool = True) -> bool:
    if not info:
        return False
    if info.get("state") == "working":
        return True
    if not recent_grace:
        return False
    last_active = info.get("last_active")
    if last_active is None:
        return False
    now = now if now is not None else time.time()
    return now - float(last_active) < BUSY_GRACE_SECONDS


def _sender_can_reach_human(frm: str) -> bool:
    sender = frm.strip().lower()
    return sender in _HUMAN or sender == "manager"


def _gate_human_message(frm: str, to: str, text: str) -> tuple[str, str, bool]:
    if to.strip().lower() not in _HUMAN or _sender_can_reach_human(frm):
        return to, text, False
    return (
        "manager",
        (
            f"[decmux human-gate | from {frm}]\n\n"
            f"{text}"
            f"{_SYS_SEP}\n"
            "A subordinate attempted to message the human directly. Review it, "
            "decide the next action, and forward a concise message with "
            '`decmux send "<text>" --to you` only if the human is needed.'
        ),
        True,
    )


def _dispatch_body(store, targets: list[tuple[str, str, str]], body: str,
                   ws_ref: str, *, frm: str = "", task_id: int | None = None) -> tuple[int, int]:
    delivered = queued = 0
    now = time.time()
    for sref, suuid, _name in targets:
        info = store.agent_by_ref(sref)
        if _should_queue(info, now):
            rowid = store.enqueue_outbox(
                surface_uuid=suuid, surface_ref=sref, body=body, frm=frm, task_id=task_id,
            )
            if rowid:
                queued += 1
            continue
        try:
            _deliver(sref, ws_ref, body)
            delivered += 1
        except (subprocess.CalledProcessError, OSError):
            pass
    if task_id and delivered:
        store.increment_task_delivered(task_id, delivered)
    return delivered, queued


def _dedupe_targets(targets: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str, str]] = []
    for target in targets:
        if target[0] in seen:
            continue
        seen.add(target[0])
        out.append(target)
    return out


def _goal_block(goal: str) -> str:
    return f"\n\nGoal:\n{goal}" if goal else ""


def task_instructions(tid: int) -> str:
    return (
        f'- comment (also serves as a progress update): decmux task comment {tid} "..."\n'
        f'- delegate: decmux task delegate {tid} <agent> "..."\n'
        f'- finish: decmux task done {tid} "..."\n'
        f'- answer: decmux task answer {tid} "..."'
    )


def _task_card(task: dict, *, triage: bool = False, frm: str = "", goal: str = "") -> str:
    tid = task["id"]
    author = frm or task.get("author") or "you"
    if triage or task.get("status") == "triage":
        return (
            f"[decmux triage #{tid} | from {author}]\n\n"
            f"{task['body']}"
            f"{_SYS_SEP}"
            f"{_goal_block(goal)}\n\n"
            "Manager action required:\n"
            "- real work must be delegated to a subordinate agent; do not solve it yourself\n"
            "- if no suitable subordinate exists, spawn one: decmux spawn --name <role> --kind <claude|codex>\n"
            f"- accept/delegate: decmux task delegate {tid} <agent> \"<instruction>\"\n"
            f"- answer directly: decmux task answer {tid} \"...\"\n"
            f"- dismiss/no action: decmux task done {tid} \"no action needed\"\n"
            f"- if reporting completion via decmux send, include: [AGENT-DONE task #{tid}]"
        )
    return (
        f"[decmux task #{tid} | {task.get('kind') or 'task'} | from {author}]\n\n"
        f"{task['body']}"
        f"{_SYS_SEP}"
        f"{_goal_block(goal)}\n\n"
        "Close this task before or with your final report. A plain status message "
        "does not close the queue item.\n"
        "Actions:\n"
        f"{task_instructions(tid)}\n"
        f'- fallback completion report: decmux send "[AGENT-DONE task #{tid}] <result>" --to manager'
    )


def _explicit_task_id(text: str) -> int | None:
    match = _TASK_REF.search(text)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _auto_close_from_report(store, *, text: str, frm: str) -> int | None:
    if frm.strip().lower() in _HUMAN or not _DONE_MARKER.search(text):
        return None
    tid = _explicit_task_id(text)
    task = None
    if tid is not None:
        try:
            task = store.get_task(tid)
        except AssertionError:
            return None
        if task["status"] in ("done", "answered"):
            return None
    else:
        candidates = store.open_tasks_for_actor(actor=frm)
        if len(candidates) != 1:
            return None
        task = candidates[0]
        tid = int(task["id"])
    store.close_task(tid, text, "done", author=frm)
    return tid


def deliver_task(store, task: dict, *, reason: str = "") -> int:
    """Deliver an existing task without creating a duplicate queue entry."""
    targets = _targets(store, task["to_whom"])
    ws_ref = _ws_ref(store)
    body = _task_card(task, goal=store.get_goal())
    if reason:
        body = f"{body}\n\n{reason}"
    delivered, _queued = _dispatch_body(
        store, targets, body, ws_ref, frm=task.get("author") or "", task_id=task["id"]
    )
    return delivered


def _task_update_targets(store, task: dict) -> list[tuple[str, str, str]]:
    targets: list[tuple[str, str, str]] = []
    targets.extend(_targets(store, "manager"))
    to_whom = (task.get("to_whom") or "").strip()
    if to_whom and to_whom.lower() not in _HUMAN | {"manager"}:
        targets.extend(_targets(store, to_whom))
    assignee = (task.get("assignee") or "").strip()
    if assignee and assignee.lower() != "manager":
        targets.extend(_targets(store, assignee))
    return _dedupe_targets(targets)


def task_update_instructions(task: dict) -> str:
    if task.get("status") in ("done", "answered"):
        return (
            "This task is CLOSED. If this comment means the work should continue, "
            f'reopen it at your discretion: decmux task reopen {int(task["id"])}'
        )
    return "Actions:\n" + task_instructions(int(task["id"]))


def deliver_task_update(store, task: dict, *, kind: str, body: str, author: str) -> dict:
    """Deliver a task comment/progress update to the manager and task owner."""
    targets = _task_update_targets(store, task)
    ws_ref = _ws_ref(store)
    message = (
        f"[decmux task #{task['id']} {kind} | from {author}]\n\n"
        f"{body}"
        f"{_SYS_SEP}\n"
        f"Status: {task.get('status') or 'unknown'}\n"
        f"Assignee: {(task.get('assignee') or 'none')}\n"
        f"{_goal_block(store.get_goal())}\n\n"
        f"Original request:\n{task['body']}\n\n"
        f"{task_update_instructions(task)}"
    )
    delivered, queued = _dispatch_body(store, targets, message, ws_ref, frm=author, task_id=None)
    return {"delivered": delivered, "queued": queued}


def deliver_goal_update(store, goal: str, *, author: str = "you") -> dict:
    goal = goal.strip()
    assert goal, "goal text required"
    targets = _targets(store, "manager")
    ws_ref = _ws_ref(store)
    body = (
        f"[decmux goal | from {author}]\n\n"
        f"{goal}"
        f"{_SYS_SEP}\n"
        "Use this as operating context for triage, delegation, review, and "
        "human-facing summaries. This is not a tracked task; delegate concrete "
        "work items separately."
    )
    delivered, queued = _dispatch_body(store, targets, body, ws_ref, frm=author, task_id=None)
    return {"delivered": delivered, "queued": queued}


def assert_manager_workflow(task: dict, *, action: str, author: str) -> None:
    if author.strip().lower() != "manager":
        return
    if action == "claim":
        raise AssertionError(
            f"manager cannot claim task #{task['id']}; delegate it to a subordinate")
    if action == "progress" and not (task.get("assignee") or "").strip():
        raise AssertionError(
            f"manager cannot progress undelegated task #{task['id']}; "
            "use decmux task delegate, answer, or done")


def deliver_manager_backlog(store, *, reason: str = "") -> dict:
    delivered = queued = skipped = 0
    for task in store.open_tasks():
        if (task.get("to_whom") or "").strip().lower() != "manager":
            continue
        if store.task_has_pending_delivery(int(task["id"])):
            skipped += 1
            continue
        before = store.task_pending_delivery_count(int(task["id"]))
        sent = deliver_task(store, task, reason=reason)
        after = store.task_pending_delivery_count(int(task["id"]))
        delivered += sent
        queued += max(0, after - before)
    return {"delivered": delivered, "queued": queued, "skipped_pending": skipped}


def delegate_task(store, tid: int, assignee: str, instruction: str,
                  *, author: str = "manager") -> dict:
    assignee = assignee.strip()
    instruction = instruction.strip()
    assert assignee, "assignee required"
    assert instruction, "instruction required"
    store.delegate_task(tid, assignee, instruction, author=author)
    task = store.get_task(tid)
    targets = _targets(store, assignee)
    assert targets, f"agent {assignee} not found"
    ws_ref = _ws_ref(store)
    body = (
        f"[decmux delegated task #{tid} | from {author}]\n\n"
        f"{instruction}"
        f"{_SYS_SEP}\n"
        f"Original request:\n{task['body']}"
        f"{_goal_block(store.get_goal())}\n\n"
        "Close this task before or with your final report.\n"
        f"{task_instructions(tid)}\n"
        f'- fallback completion report: decmux send "[AGENT-DONE task #{tid}] <result>" --to manager'
    )
    delivered, queued = _dispatch_body(store, targets, body, ws_ref, frm=author, task_id=tid)
    store.add_chat(frm=author, dst=assignee,
                   body=f"delegated task #{tid}: {instruction}", kind="report")
    return {"delivered": delivered, "queued": queued, "assignee": assignee}


def send(store, text: str, to: str = "manager", frm: str | None = None,
         track_task: bool | None = None, attachments: list | None = None,
         force: bool = False) -> dict:
    frm = frm or resolve_sender(store)
    requested_to = to
    to, text, gated_to_manager = _gate_human_message(frm, to, text)
    # Attachments reach agents only as a path reference (cmux send is text-only);
    # the bytes live under the store's files/ and the agent Reads them by path.
    if attachments:
        refs = [f"[attachment: {a.get('name', 'file')} -> {p}]"
                for a in attachments
                if (p := store.file_abspath(a.get("id", "")))]
        if refs:
            text = (text + "\n" + "\n".join(refs)).strip()
    sender = frm.strip().lower()
    targets = _targets(store, to)
    # Downward guard: withhold a status-only message the manager aimed at
    # subordinate(s) so it doesn't interrupt their work. Keep it on the timeline
    # (kind='report') and let the caller warn the manager to resend with force=True
    # if it was actually a command. Conservative by design — never blocks a directive.
    if not force and _is_downward(frm, to) and _looks_like_status_only(text):
        store.add_chat(frm=frm, dst=to, body=text, kind="report")
        store.commit()
        return {"frm": frm, "dst": to, "requested_dst": requested_to,
                "delivered": 0, "queued": 0, "task": None, "closed_task": None,
                "gated_to_manager": gated_to_manager, "withheld_status": True}
    if sender in _HUMAN and text.strip().lower().startswith("/goal "):
        goal = text.strip()[6:].strip()
        assert goal, "goal text required"
        store.set_goal(goal)
        store.add_chat(frm=frm, dst="manager", body=f"goal set: {goal}", kind="report")
        res = deliver_goal_update(store, goal, author=frm)
        store.commit()
        return {"frm": frm, "dst": "manager", "requested_dst": requested_to,
                "delivered": res["delivered"], "queued": res["queued"], "task": None,
                "closed_task": None, "gated_to_manager": gated_to_manager, "goal": True}
    # Only a human message becomes a tracked item, and it lands as TRIAGE — the
    # manager judges it (accept as work / answer / dismiss), decmux never silently
    # files work. Agent->agent/manager traffic is routed but never auto-tasked.
    should_track = (sender in _HUMAN) if track_task is None else track_task
    tid = None
    if should_track and to.strip().lower() not in _HUMAN:
        kind = "question" if text.strip().endswith("?") else "command"
        tid = store.add_task(kind=kind, body=text, to_whom=to,
                             source="chat", author=frm, status="triage")
    # "chat" = human-facing conversation (to/from the human); "report" = operational
    # agent<->agent/manager routing, which belongs in the flow, not the human chat.
    human_facing = sender in _HUMAN or to.strip().lower() in _HUMAN
    store.add_chat(frm=frm, dst=to, body=text,
                   kind=("chat" if human_facing else "report"))
    closed_task = _auto_close_from_report(store, text=text, frm=frm)
    store.commit()
    if to.strip().lower() in _HUMAN:
        try:
            cmux.run("notify", "--title", f"{frm} -> you", "--body", text[:150])
        except (subprocess.CalledProcessError, OSError):
            pass
        return {"frm": frm, "dst": to, "requested_dst": requested_to,
                "delivered": 0, "queued": 0, "task": tid, "closed_task": closed_task,
                "gated_to_manager": gated_to_manager}
    if tid:
        body = _task_card(store.get_task(tid), triage=True, frm=frm, goal=store.get_goal())
    else:
        body = (f"[decmux · from {frm}]\n\n{text}{_SYS_SEP}\n"
                f'reply: decmux send "<text>" --to {frm}')
    ws_ref = _ws_ref(store)
    delivered, queued = _dispatch_body(store, targets, body, ws_ref, frm=frm, task_id=tid)
    store.commit()
    return {"frm": frm, "dst": to, "requested_dst": requested_to,
            "delivered": delivered, "queued": queued, "task": tid,
            "closed_task": closed_task, "gated_to_manager": gated_to_manager}


def flush_outbox(store, surface_uuid: str, surface_ref: str, ws_ref: str,
                 limit: int = 1) -> int:
    """Deliver messages queued while a surface was busy, now that it is idle.
    Marks only the ones actually delivered; stops on the first failure so the
    rest are retried on the next idle tick."""
    if _should_queue(store.agent_by_ref(surface_ref), recent_grace=False):
        return 0
    sent_ids: list[int] = []
    task_counts: dict[int, int] = {}
    for row in store.pending_outbox(surface_uuid, limit=limit):
        try:
            _deliver(surface_ref, ws_ref, row["body"])
        except (subprocess.CalledProcessError, OSError):
            break
        sent_ids.append(row["id"])
        if row.get("task_id"):
            task_counts[int(row["task_id"])] = task_counts.get(int(row["task_id"]), 0) + 1
    store.mark_outbox_delivered(sent_ids)
    for task_id, count in task_counts.items():
        store.increment_task_delivered(task_id, count)
    return len(sent_ids)


AGENT_CMD = {"claude": "claude --dangerously-skip-permissions", "codex": "codex --yolo"}


def _surface_window(surface_ref: str) -> str | None:
    """The window a surface lives in (for clustering spawns in the manager's window)."""
    try:
        c = cmux.run_json("identify", "--surface", surface_ref, "--id-format", "both",
                          "--json")["caller"]
        return c.get("window_ref")
    except (subprocess.CalledProcessError, OSError, KeyError):
        return None


def spawn_agent(store, *, name: str | None = None, kind: str = "claude",
                manager: bool = False, command: str | None = None,
                direction: str = "right") -> dict:
    """Split a new pane and launch a decmux-managed agent in it.

    Workers split into the manager's window so the team clusters there. Records the
    surface in the managed registry, sets DECMUX_ROLE + the cmux guard, binds the
    manager if requested, and onboards a codex agent via the protocol."""
    wl = cmux.run_json("workspace", "list", "--id-format", "both", "--json")["workspaces"]
    w = next((x for x in wl if x.get("id") == store.workspace_uuid), None)
    assert w, "workspace not found"
    ws_ref, cwd = w["ref"], w.get("current_directory", "")
    mgr = store.manager()
    if manager and mgr:
        return {"created": False, "reason": "manager already bound", "surface_ref": mgr[1]}
    # split (new-pane); a worker goes into the manager's window so agents cluster there
    args = ["new-pane", "--type", "terminal", "--direction", direction,
            "--workspace", ws_ref, "--focus", "false"]
    if mgr and not manager:
        window = _surface_window(mgr[1])
        if window:
            args += ["--window", window]
    out = cmux.run(*args)                       # "OK surface:N pane:M workspace:W"
    mref = re.search(r"(surface:\d+)", out)
    assert mref, f"could not parse new-pane output: {out!r}"
    sref = mref.group(1)
    suuid = cmux.run_json("identify", "--surface", sref, "--id-format", "both",
                          "--json")["caller"]["surface_id"]
    nm = name or ("manager" if manager else "agent")
    cmux.run("rename-tab", "--workspace", ws_ref, "--surface", sref, nm)
    store.mark_managed(suuid, role=("manager" if manager else "agent"), kind=kind)
    cmd = assets.guarded_command(
        command or AGENT_CMD.get(kind or "claude", AGENT_CMD["claude"]),
        env={"CMUX_WORKSPACE_ID": store.workspace_uuid, "CMUX_WORKSPACE_REF": ws_ref,
             "CMUX_SURFACE_ID": suuid, "CMUX_SURFACE_REF": sref,
             "DECMUX_ROLE": "manager" if manager else "agent"},
        cwd=cwd or None)
    cmux.run("send", "--workspace", ws_ref, "--surface", sref, cmd)
    cmux.run("send-key", "--workspace", ws_ref, "--surface", sref, "Enter")
    if kind == "codex":                  # claude gets the protocol via the SessionStart hook
        deliver_protocol(store, suuid, sref)
    if manager:
        store.bind_manager(surface_uuid=suuid, surface_ref=sref, cwd=cwd)
    store.commit()
    return {"created": True, "name": nm, "surface_ref": sref, "manager": manager}


def deliver_protocol(store, surface_uuid: str, surface_ref: str) -> int:
    """Onboard a non-Claude agent (e.g. codex) by queuing the decmux protocol once.

    Claude agents receive the protocol via the SessionStart hook; this is the
    equivalent channel for agents without that mechanism. Queued to the outbox so
    it lands de-mixed when the agent is idle."""
    return store.enqueue_outbox(surface_uuid=surface_uuid, surface_ref=surface_ref,
                                body=assets.PROTOCOL, frm="decmux")

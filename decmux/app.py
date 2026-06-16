"""Interactive control program.

`decmux` with no args opens this: a prompt-toolkit REPL with a persistent bottom
prompt + live status toolbar, while a background thread runs supervision and
another tails the store so manager->you messages and state transitions appear in
real time *above* the prompt (patch_stdout) instead of fighting it.

It is deliberately a line REPL, not a full-screen TUI — cmux stays the window to
watch real agent surfaces; this is the de-mixed input channel plus live signals.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import Counter

from . import bus
from . import session as session_mod
from .store import Store

_COMMANDS = ["/spawn-manager", "/spawn", "/despawn", "/goal", "/to", "/status", "/tasks",
             "/task", "/new", "/feed", "/report", "/help", "/quit"]
_TARGETS = ["manager", "human", "all"]

# shown beside each completion (prompt_toolkit display_meta) when it is focused
_CMD_META = {
    "/spawn-manager": "create the manager in a new surface",
    "/spawn": "add a worker  (/spawn <name> [short|long|full])",
    "/despawn": "release an agent  (/despawn <name> [now])",
    "/goal": "set the workspace goal (briefs the manager)",
    "/to": "set message target (manager | you | <agent> | all)",
    "/status": "agent states (from the supervisor)",
    "/tasks": "open tasks  (/tasks closed for finished)",
    "/task": "focus a task thread + show its timeline  (/task <id>)",
    "/new": "start a new thread (fresh task)  (/new [text])",
    "/feed": "recent human-facing chat  (/feed N)",
    "/report": "recent state transitions  (/report N)",
    "/help": "list commands",
    "/quit": "exit (supervision stops)",
}
_TARGET_META = {
    "manager": "the bound manager",
    "human": "the human (refined updates only)",
    "all": "broadcast to all agents",
}


def _completions(store) -> tuple[list[str], dict[str, str]]:
    """Completion words + their descriptions (commands, targets, live agent names)."""
    names = [bus._clean_name(a["title"]) for a in store.list_agents()]
    words = _COMMANDS + _TARGETS + names
    meta = {**_CMD_META, **_TARGET_META}
    for n in names:
        meta.setdefault(n, "agent")
    return words, meta

HELP = """commands:  (Enter sends · Shift+Enter or Alt+Enter = newline)
  <text>            send to target; to the manager it continues the current thread
  /new [text]       start a new thread (fresh task)
  /task <id>        focus a task thread + show its timeline
  /tasks [closed]   open tasks (or finished ones)
  /spawn-manager    create the manager in a new surface
  /spawn [name] [t] add a worker (t = short|long|full term; default short)
  /despawn <name>   release an agent (graceful; add 'now' to close at once)
  /goal <text>      set the workspace goal (briefs the manager)
  /to <name>        set target (manager | human | <agent> | all)
  /status           agent states (from the supervisor)
  /feed [n]         recent human-facing chat
  /report [n]       recent state transitions
  /help  /quit"""

_GLYPH = {"working": "●", "idle": "○", "stuck": "▲", "error": "✖",
          "dead": "☠", "budget": "$", "blocked-on-decision": "?"}


class AppState:
    def __init__(self, store: Store) -> None:
        # This Store belongs to the thread that constructed AppState (the prompt
        # loop). Other threads (supervision, feed poller) use their own.
        self.store = store
        self.workspace_uuid = store.workspace_uuid
        self.target = "manager"
        self.thread: int | None = None     # current task thread (Q&A continues here)
        self.running = True


def _task_open(store, tid: int) -> bool:
    try:
        return store.get_task(tid)["status"] not in _CLOSED
    except AssertionError:
        return False


def _int(rest: str, default: int) -> int:
    try:
        return int(rest)
    except ValueError:
        return default


def _status(store) -> None:
    agents = store.list_agents()
    if not agents:
        print("  (no agents in this workspace)")
        return
    kinds = store.managed_kinds()
    cwd, u = store.get_meta("cwd"), store.usage()
    head = []
    if cwd:
        head.append(f"dir {cwd}")
    if u.get("turns") or u.get("tools"):
        head.append(f"usage {u.get('turns') or 0} turns / {u.get('tools') or 0} tools")
    if head:
        print("  " + "   ·   ".join(head))
    for a in agents:
        bk = f"·{a['busy_kind']}" if a.get("busy_kind") else ""
        kind = kinds.get(a["surface_uuid"], "")
        model = a.get("model") or ""
        eff = f" [{a['effort']}]" if a.get("effort") else ""
        print(f"  {(a['state'] or '?') + bk:16} {bus._clean_name(a['title']):18} "
              f"{a['surface_ref']:11} {kind:7} {model}{eff}")


_CLOSED = {"done", "answered"}


def _tasks(store, closed: bool = False) -> None:
    rows = [t for t in store.list_tasks() if (t["status"] in _CLOSED) == closed]
    if not rows:
        print("  (no closed tasks)" if closed else "  (no open tasks — /tasks closed for finished)")
        return
    print(f"  {'closed' if closed else 'open'} tasks:")
    for t in rows:
        who = f" @{t['assignee']}" if t.get("assignee") else ""
        if closed:
            tail = f"  => {(t['result'] or '').strip()[:44]}" if t.get("result") else ""
            print(f"  #{t['id']} [{t['status']}]{who} {t['body'][:46]}{tail}")
        else:
            prog = [ln for ln in (t.get("progress") or "").splitlines() if ln.strip()]
            last = f"   · {prog[-1].lstrip('• ')[:46]}" if prog else ""
            print(f"  #{t['id']} [{t['status']}]{who} -> {t['to_whom']}: {t['body'][:46]}{last}")
    if not closed:
        n = sum(1 for t in store.list_tasks() if t["status"] in _CLOSED)
        if n:
            print(f"  ({n} closed — /tasks closed · /task <id> for detail)")


def _task_detail(store, tid: int) -> None:
    try:
        t = store.get_task(tid)
    except AssertionError:
        print(f"  no task #{tid}")
        return
    who = f" @{t['assignee']}" if t.get("assignee") else ""
    print(f"  #{t['id']} [{t['status']}] {t.get('kind') or 'task'} -> {t['to_whom']}{who}")
    print(f"  {t['body']}")
    if t.get("result"):
        print(f"  result: {t['result']}")
    print("  timeline:")
    for c in store.task_comments(tid):
        ts = time.strftime("%H:%M", time.localtime(c["ts"]))
        print(f"    {ts}  {c['author']} [{c['kind']}]  {c['body'][:70]}")


def _feed(store, n: int) -> None:
    for c in store.recent_chat(kind="chat", limit=n):
        print(f"  {c['frm']} -> {c['dst']}: {c['body'][:100]}")


def _report(store, n: int) -> None:
    for t in store.recent_transitions(n):
        print(f"  {t['from_state']} -> {t['to_state']}  {t['title']}")


def _handle(st: AppState, line: str) -> bool:
    """Process one input line. Returns False to quit."""
    line = line.strip()
    if not line:
        return True
    if line.startswith("/"):
        cmd, _, rest = line[1:].partition(" ")
        rest = rest.strip()
        if cmd in ("quit", "exit", "q"):
            return False
        if cmd == "help":
            print(HELP)
        elif cmd == "to":
            if not rest:
                print("usage: /to <manager | human | all | agent>")
            else:
                st.target = rest
                print(f"target -> {st.target}")
        elif cmd == "status":
            _status(st.store)
        elif cmd == "tasks":
            _tasks(st.store, closed=(rest == "closed"))
        elif cmd == "task":
            if rest.isdigit():
                st.thread = int(rest)            # focus this thread
                _task_detail(st.store, int(rest))
            else:
                print("usage: /task <id>")
        elif cmd == "new":
            st.thread = None
            if rest:
                res = bus.send(st.store, rest, to="manager", frm="human")
                st.thread = res.get("task")
                print(f"-> new #{st.thread} (delivered {res.get('delivered', 0)}, "
                      f"queued {res.get('queued', 0)})")
            else:
                print("new thread — your next message starts a fresh task")
        elif cmd == "feed":
            _feed(st.store, _int(rest, 20))
        elif cmd == "report":
            _report(st.store, _int(rest, 20))
        elif cmd == "goal":
            if not rest:
                print("usage: /goal <text>")
            else:
                res = bus.send(st.store, "/goal " + rest, to="manager", frm="human")
                print(f"goal set (delivered {res.get('delivered', 0)}, queued {res.get('queued', 0)})")
        elif cmd in ("spawn", "spawn-manager"):
            parts = rest.split()
            term = parts.pop() if parts and parts[-1] in ("short", "long", "full") else "short"
            nm = " ".join(parts) or None
            # REPL spawns are human-origin: never auto-reaped (you confirm via /despawn)
            res = bus.spawn_agent(st.store, name=nm, manager=(cmd == "spawn-manager"),
                                  term=term, origin="human")
            if res.get("created"):
                label = "manager" if res["manager"] else res["name"]
                tag = "" if res["manager"] else f" [{res.get('term', 'short')}]"
                print(f"spawned {label}: {res['surface_ref']}{tag} "
                      f"(switch to it in cmux to watch)")
                if res["manager"]:
                    st.target = "manager"
            else:
                print(res.get("reason", "not created"))
        elif cmd == "despawn":
            if not rest:
                print("usage: /despawn <agent> [now]")
            else:
                parts = rest.split()
                now = bool(parts) and parts[-1] == "now"
                if now:
                    parts.pop()
                res = bus.despawn(st.store, " ".join(parts), now=now)
                print(f"despawned {res['name']} — surface closed" if res["closed"]
                      else f"releasing {res['name']} — closed when idle & handed off")
        else:
            print(f"unknown command: /{cmd}  (try /help)")
        return True
    # chatting the manager continues the current task thread (re-briefed each time)
    if st.target == "manager" and st.thread is not None and _task_open(st.store, st.thread):
        res = bus.continue_thread(st.store, st.thread, line, frm="human")
        print(f"-> #{st.thread} (delivered {res['delivered']}, queued {res['queued']})")
        return True
    res = bus.send(st.store, line, to=st.target, frm="human")
    if res.get("withheld_status"):
        print("withheld (status-only downward); use the agent's name or --force")
        return True
    if st.target == "manager" and res.get("task"):
        st.thread = res["task"]            # a fresh message to the manager opens a thread
    tid = f" #{res['task']}" if res.get("task") else ""
    extra = " [gated->manager]" if res.get("gated_to_manager") else ""
    print(f"-> {res['dst']}{tid} (delivered {res['delivered']}, "
          f"queued {res.get('queued', 0)}){extra}")
    return True


def _feed_poller(st: AppState) -> None:
    """Tail the store; print new manager->you messages and alert transitions.

    Uses its own Store connection (sqlite connections are per-thread)."""
    store = Store(st.workspace_uuid)
    last_chat = store.last_chat_id()
    last_tr = store.last_transition_id()
    while st.running:
        try:
            for c in store.chat_after(last_chat, kind="chat"):
                last_chat = c["id"]
                if c["frm"] != "human":           # incoming, not our own echo
                    print(f"[{c['frm']}] {c['body']}")
            for t in store.transitions_after(last_tr):
                last_tr = t["id"]
                print(f"{_GLYPH.get(t['to_state'], '·')} {bus._clean_name(t['title'] or '')} "
                      f"-> {t['to_state']}")
        except sqlite3.OperationalError:
            pass   # a momentary lock must never kill the display thread
        time.sleep(1.5)


def _startup_guide(store) -> None:
    """When there's no team yet, show the concrete first steps instead of a blank prompt."""
    if store.manager() or store.managed_set():
        return
    print(
        "\nNo agents yet. Build a team right here:\n"
        "  /spawn-manager       create the manager (a Claude that runs the team)\n"
        "  /spawn <name>        add a worker agent\n"
        "then:  /goal <text>  to set the objective, then type to message the manager.\n"
        "(or convert a surface you already opened: run `decmux agent --manager` / `decmux agent` there)\n"
    )


def _toolbar(st: AppState) -> str:
    counts = Counter(a["state"] for a in st.store.list_agents())
    parts = "  ".join(f"{_GLYPH.get(s, '·')}{n}" for s, n in counts.items()) or "no agents"
    goal = st.store.get_goal()
    tail = f"  goal: {goal[:32]}" if goal else ""
    return f" decmux  {parts}  open:{len(st.store.open_tasks())}  ->{st.target}{tail} "


def repl(workspace_uuid: str, *, notify: bool = True) -> int:
    import shutil as _shutil
    import sys as _sys

    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.styles import Style

    # A terminal only emits a distinct code for Shift+Enter under an enhanced
    # keyboard protocol; we enable modifyOtherKeys level 1 below (backward
    # compatible — other keys keep legacy codes). Teach prompt_toolkit to decode
    # both the modifyOtherKeys and kitty Shift+Enter encodings.
    for _seq in ("\x1b[27;2;13~", "\x1b[13;2u"):
        ANSI_SEQUENCES[_seq] = Keys.F24

    st = AppState(Store(workspace_uuid))   # this thread's connection
    holder: dict = {}

    def _supervise() -> None:
        # build the Session in its own thread so its store connection lives here
        s = session_mod.Session(workspace_uuid, notify=notify, pin=True)
        holder["sess"] = s
        s.run()

    threading.Thread(target=_supervise, daemon=True).start()
    threading.Thread(target=_feed_poller, args=(st,), daemon=True).start()

    kb = KeyBindings()

    @kb.add("enter")
    def _(event):                       # Enter sends
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _(event):                       # Alt+Enter (Esc then Enter) inserts a newline
        event.current_buffer.insert_text("\n")

    @kb.add(Keys.F24)
    def _(event):                       # Shift+Enter, when the terminal reports it
        event.current_buffer.insert_text("\n")

    def message():
        w = max(20, _shutil.get_terminal_size((80, 24)).columns)
        tag = f" #{st.thread}" if st.thread is not None else ""
        return FormattedText([("class:sep", "─" * w + "\n"),
                              ("class:pr", f"decmux[{st.target}{tag}]> ")])

    style = Style.from_dict({"sep": "fg:#666666", "pr": "bold"})
    psession: PromptSession = PromptSession(multiline=True, key_bindings=kb, style=style)
    _sys.stdout.write("\x1b[>4;1m")     # ask the terminal to report Shift+Enter (modifyOtherKeys L1)
    _sys.stdout.flush()
    print(f"decmux — workspace {workspace_uuid}. supervising in the background.")
    print("Enter sends · Shift+Enter / Alt+Enter for a newline · /help · /quit")
    _startup_guide(st.store)
    try:
        with patch_stdout():
            while True:
                words, meta = _completions(st.store)
                completer = WordCompleter(words, meta_dict=meta, sentence=True)
                try:
                    line = psession.prompt(message(), completer=completer,
                                           bottom_toolbar=lambda: _toolbar(st))
                except (EOFError, KeyboardInterrupt):
                    break
                if not _handle(st, line):
                    break
    finally:
        st.running = False
        _sys.stdout.write("\x1b[>4;0m")   # restore the terminal's keyboard mode
        _sys.stdout.flush()
        if holder.get("sess") is not None:
            holder["sess"].close()
    return 0

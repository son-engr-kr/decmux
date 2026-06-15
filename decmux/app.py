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

_COMMANDS = ["/spawn-manager", "/spawn", "/goal", "/to", "/status", "/tasks",
             "/feed", "/report", "/help", "/quit"]
_TARGETS = ["manager", "you", "all"]

# shown beside each completion (prompt_toolkit display_meta) when it is focused
_CMD_META = {
    "/spawn-manager": "create the manager in a new surface",
    "/spawn": "add a worker agent in a new surface  (/spawn <name>)",
    "/goal": "set the workspace goal (briefs the manager)",
    "/to": "set message target (manager | you | <agent> | all)",
    "/status": "agent states (from the supervisor)",
    "/tasks": "open tasks",
    "/feed": "recent human-facing chat  (/feed N)",
    "/report": "recent state transitions  (/report N)",
    "/help": "list commands",
    "/quit": "exit (supervision stops)",
}
_TARGET_META = {
    "manager": "the bound manager",
    "you": "the human (refined updates only)",
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

HELP = """commands:
  <text>            send to the current target (default: manager)
  /spawn-manager    create the manager in a new surface
  /spawn [name]     add a worker agent in a new surface
  /goal <text>      set the workspace goal (briefs the manager)
  /to <name>        set target (manager | you | <agent> | all)
  /status           agent states (from the supervisor)
  /tasks            open tasks
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
        self.running = True


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
    for a in agents:
        bk = f"·{a['busy_kind']}" if a.get("busy_kind") else ""
        print(f"  {(a['state'] or '?') + bk:18} {bus._clean_name(a['title']):24} {a['surface_ref']}")


def _tasks(store) -> None:
    rows = store.open_tasks()
    if not rows:
        print("  (no open tasks)")
        return
    for t in rows:
        print(f"  #{t['id']} [{t['status']}] -> {t['to_whom']}: {t['body'][:70]}")


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
                print("usage: /to <manager | you | all | agent>")
            else:
                st.target = rest
                print(f"target -> {st.target}")
        elif cmd == "status":
            _status(st.store)
        elif cmd == "tasks":
            _tasks(st.store)
        elif cmd == "feed":
            _feed(st.store, _int(rest, 20))
        elif cmd == "report":
            _report(st.store, _int(rest, 20))
        elif cmd == "goal":
            if not rest:
                print("usage: /goal <text>")
            else:
                res = bus.send(st.store, "/goal " + rest, to="manager", frm="you")
                print(f"goal set (delivered {res.get('delivered', 0)}, queued {res.get('queued', 0)})")
        elif cmd in ("spawn", "spawn-manager"):
            res = bus.spawn_agent(st.store, name=(rest or None), manager=(cmd == "spawn-manager"))
            if res.get("created"):
                label = "manager" if res["manager"] else res["name"]
                print(f"spawned {label}: {res['surface_ref']} "
                      f"(switch to it in cmux to watch)")
                if res["manager"]:
                    st.target = "manager"
            else:
                print(res.get("reason", "not created"))
        else:
            print(f"unknown command: /{cmd}  (try /help)")
        return True
    res = bus.send(st.store, line, to=st.target, frm="you")
    if res.get("withheld_status"):
        print("withheld (status-only downward); use the agent's name or --force")
        return True
    extra = " [gated->manager]" if res.get("gated_to_manager") else ""
    print(f"-> {res['dst']} (delivered {res['delivered']}, queued {res.get('queued', 0)}){extra}")
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
                if c["frm"] != "you":           # incoming, not our own echo
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
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.patch_stdout import patch_stdout

    st = AppState(Store(workspace_uuid))   # this thread's connection
    holder: dict = {}

    def _supervise() -> None:
        # build the Session in its own thread so its store connection lives here
        s = session_mod.Session(workspace_uuid, notify=notify, pin=True)
        holder["sess"] = s
        s.run()

    threading.Thread(target=_supervise, daemon=True).start()
    threading.Thread(target=_feed_poller, args=(st,), daemon=True).start()
    psession: PromptSession = PromptSession()
    print(f"decmux — workspace {workspace_uuid}. supervising in the background. /help · /quit")
    _startup_guide(st.store)
    try:
        with patch_stdout():
            while True:
                words, meta = _completions(st.store)
                completer = WordCompleter(words, meta_dict=meta, sentence=True)
                try:
                    line = psession.prompt(f"decmux[{st.target}]> ",
                                           completer=completer,
                                           bottom_toolbar=lambda: _toolbar(st))
                except (EOFError, KeyboardInterrupt):
                    break
                if not _handle(st, line):
                    break
    finally:
        st.running = False
        if holder.get("sess") is not None:
            holder["sess"].close()
    return 0

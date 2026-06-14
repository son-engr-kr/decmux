"""Interactive control program (line REPL).

`decmux` with no args opens this: you chat to the manager and run `/commands`,
while the supervision session runs in a background thread. Deliberately a
line-oriented REPL, not a full-screen TUI — cmux is the window; this is just the
de-mixed input channel plus on-demand views.

Incoming manager->you messages fire a desktop `cmux notify` (from the bus) and are
visible with `/feed`, so there are no async prints fighting the prompt.
"""

from __future__ import annotations

import threading

from . import bus
from . import session as session_mod

HELP = """commands:
  <text>            send <text> to the current target (default: manager)
  /to <name>        set the target (manager | you | <agent> | all)
  /status           agent states (from the supervisor)
  /tasks            open tasks
  /feed [n]         recent human-facing chat
  /report [n]       recent state transitions
  /goal <text>      set the workspace goal (briefs the manager)
  /help             this help
  /quit             exit (supervision stops)"""


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


def _int(rest: str, default: int) -> int:
    try:
        return int(rest)
    except ValueError:
        return default


def repl(workspace_uuid: str, *, notify: bool = True) -> int:
    sess = session_mod.Session(workspace_uuid, notify=notify)
    threading.Thread(target=sess.run, daemon=True).start()
    store = sess.store
    target = "manager"
    print(f"decmux — workspace {workspace_uuid}. supervising in the background.")
    print("type a message for the manager; /help for commands; /quit to exit.")
    try:
        while True:
            try:
                line = input(f"decmux[{target}]> ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line.startswith("/"):
                cmd, _, rest = line[1:].partition(" ")
                rest = rest.strip()
                if cmd in ("quit", "exit", "q"):
                    break
                elif cmd == "help":
                    print(HELP)
                elif cmd == "to" and rest:
                    target = rest
                    print(f"target -> {target}")
                elif cmd == "status":
                    _status(store)
                elif cmd == "tasks":
                    _tasks(store)
                elif cmd == "feed":
                    _feed(store, _int(rest, 20))
                elif cmd == "report":
                    _report(store, _int(rest, 20))
                elif cmd == "goal" and rest:
                    res = bus.send(store, "/goal " + rest, to="manager", frm="you")
                    print(f"goal set (delivered {res.get('delivered', 0)}, "
                          f"queued {res.get('queued', 0)})")
                else:
                    print("unknown command; /help")
            else:
                res = bus.send(store, line, to=target, frm="you")
                if res.get("withheld_status"):
                    print("withheld (status-only downward); use the agent's name or --force")
                    continue
                extra = " [gated->manager]" if res.get("gated_to_manager") else ""
                print(f"-> {res['dst']} (delivered {res['delivered']}, "
                      f"queued {res.get('queued', 0)}){extra}")
    finally:
        sess.close()
    return 0

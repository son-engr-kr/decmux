"""Interactive control program.

`decmux` with no args opens this: a prompt-toolkit REPL with a persistent bottom
prompt + live status toolbar, while a background thread runs supervision and
another tails the store so manager->you messages and state transitions appear in
real time *above* the prompt (patch_stdout) instead of fighting it.

It is deliberately a line REPL, not a full-screen TUI — cmux stays the window to
watch real agent surfaces; this is the de-mixed input channel plus live signals.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import Counter

from . import bus
from . import session as session_mod
from .store import Store

_COMMANDS = ["/spawn-manager", "/spawn", "/despawn", "/goal", "/to", "/status", "/tasks",
             "/task", "/new", "/feed", "/report", "/usage", "/help", "/quit"]
_TARGETS = ["manager", "human", "all"]

# shown beside each completion (prompt_toolkit display_meta) when it is focused
_CMD_META = {
    "/spawn-manager": "create the manager in a new surface",
    "/spawn": "add a worker  (/spawn <name> [short|long|full])",
    "/despawn": "release an agent  (/despawn <name> [now])",
    "/goal": "set the goal & run the autonomous loop toward it (alone: show it)",
    "/to": "set message target (manager | you | <agent> | all)",
    "/status": "agent states (from the supervisor)",
    "/tasks": "browse tasks: ↑/↓ + live detail  (/tasks closed · list for plain)",
    "/task": "focus a task thread + show its timeline  (/task <id>)",
    "/new": "start a new thread (fresh task)  (/new [text])",
    "/feed": "recent human-facing chat  (/feed N)",
    "/report": "recent state transitions  (/report N)",
    "/usage": "5h activity trend (sparkline) + at-this-rate projection",
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
  /tasks            browse tasks (↑/↓, live detail, Enter focuses)  ·  closed/list
  /spawn-manager    create the manager in a new surface
  /spawn [name] [t] add a worker (t = short|long|full term; default short)
  /despawn <name>   release an agent (graceful; add 'now' to close at once)
  /goal <text>      set the goal & run the autonomous loop toward it (/goal = show)
  /to <name>        set target (manager | human | <agent> | all)
  /status           agent states (from the supervisor)
  /feed [n]         recent human-facing chat
  /report [n]       recent state transitions
  /usage            5h activity trend + at-this-rate projection
  /help  /quit"""

_GLYPH = {"working": "●", "idle": "○", "stuck": "▲", "error": "✖",
          "dead": "☠", "budget": "$", "blocked-on-decision": "?"}

# --- ANSI palette: the REPL is a TTY; flat white is the readability complaint. ---
# Honors NO_COLOR. Wrap whole strings (the SGR codes reset at the end) so column
# widths are padded BEFORE coloring.
_NO_COLOR = os.environ.get("NO_COLOR") is not None


def _sgr(code: str, s: str) -> str:
    return s if _NO_COLOR else f"\x1b[{code}m{s}\x1b[0m"


def _dim(s):  return _sgr("2", s)
def _b(s):    return _sgr("1", s)
def _cyan(s): return _sgr("36", s)
def _grn(s):  return _sgr("32", s)
def _yel(s):  return _sgr("33", s)
def _red(s):  return _sgr("31", s)
def _mag(s):  return _sgr("35", s)
def _gray(s): return _sgr("90", s)

_STATE_SGR = {"working": "32", "idle": "36", "stuck": "33", "error": "31",
              "dead": "31", "budget": "35", "blocked-on-decision": "33"}
_TASK_SGR = {"triage": "33", "open": "36", "in_progress": "33",
             "done": "32", "answered": "32"}


def _state_c(state: str, s: str | None = None) -> str:
    return _sgr(_STATE_SGR.get(state, "37"), s if s is not None else (state or "?"))


def _task_c(status: str, s: str | None = None) -> str:
    return _sgr(_TASK_SGR.get(status, "37"), s if s is not None else status)


def _sender_c(frm: str) -> str:
    f = (frm or "").lower()
    if f == "manager":
        return _cyan(frm)
    if f == "decmux":
        return _yel(frm)
    if f in ("human", "you", "user", "me"):
        return _grn(frm)
    return _mag(frm)                       # worker agents


def _render_message(frm: str, body: str, dst: str | None = None) -> None:
    """A routed message, readably: a colored sender header + a left gutter that
    preserves the original line breaks (the 'formatting is ignored' complaint)."""
    head = _b(_sender_c(frm))
    if dst and dst.lower() not in ("human", "you"):
        head += _gray(f" → {dst}")
    print()
    print(head)
    for ln in (body or "").splitlines() or [""]:
        print(_gray(" │ ") + ln)


_SPARK = "▁▂▃▄▅▆▇█"


def _spark(values: list[int]) -> str:
    if not values:
        return ""
    mx = max(values)
    if not mx:
        return "·" * len(values)
    return "".join("·" if v == 0 else _SPARK[min(7, int(v / mx * 7 + 0.5))] for v in values)


def _spark_fixed(values, vmax: float = 100.0) -> str:
    """Sparkline on a fixed 0..vmax scale (None = gap), for absolute series like %."""
    return "".join("·" if v is None else _SPARK[min(7, max(0, int(v / vmax * 7 + 0.5)))]
                   for v in values)


def _color_pct(used: float, s: str) -> str:
    return _red(s) if used >= 85 else _yel(s) if used >= 60 else _grn(s)


class AppState:
    def __init__(self, store: Store) -> None:
        # This Store belongs to the thread that constructed AppState (the prompt
        # loop). Other threads (supervision, feed poller) use their own.
        self.store = store
        self.workspace_uuid = store.workspace_uuid
        self.target = "manager"
        self.thread: int | None = None     # current task thread (Q&A continues here)
        self.running = True
        self.browsing = False              # true while the full-screen task browser owns the screen


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
        print(_gray("  (no agents in this workspace)"))
        return
    kinds = store.managed_kinds()
    cwd, u = store.get_meta("cwd"), store.usage()
    head = []
    if cwd:
        head.append(f"dir {cwd}")
    if u.get("turns") or u.get("tools"):
        head.append(f"usage {u.get('turns') or 0} turns / {u.get('tools') or 0} tools")
    if head:
        print(_gray("  " + "   ·   ".join(head)))
    for a in agents:
        bk = f"·{a['busy_kind']}" if a.get("busy_kind") else ""
        glyph = _GLYPH.get(a["state"], "·")
        st_txt = f"{glyph} {(a['state'] or '?')}{bk}".ljust(18)
        model = (a.get("model") or "") + (f" [{a['effort']}]" if a.get("effort") else "")
        print("  " + _state_c(a["state"], st_txt) + " "
              + _b(bus._clean_name(a["title"]).ljust(18)) + " "
              + _gray((a["surface_ref"] or "").ljust(11)) + " "
              + _gray(kinds.get(a["surface_uuid"], "").ljust(7)) + " " + _dim(model))


_CLOSED = {"done", "answered"}


def _tasks(store, closed: bool = False) -> None:
    rows = [t for t in store.list_tasks() if (t["status"] in _CLOSED) == closed]
    if not rows:
        print(_gray("  (no closed tasks)" if closed
                    else "  (no open tasks — /tasks closed for finished)"))
        return
    print(_b(f"  {'closed' if closed else 'open'} tasks:"))
    for t in rows:
        who = _gray(f" @{t['assignee']}") if t.get("assignee") else ""
        idtag = _b(f"#{t['id']}") + " " + _task_c(t["status"], f"[{t['status']}]")
        if closed:
            tail = _dim(f"  => {(t['result'] or '').strip()[:44]}") if t.get("result") else ""
            print(f"  {idtag}{who} {t['body'][:46]}{tail}")
        else:
            prog = [ln for ln in (t.get("progress") or "").splitlines() if ln.strip()]
            last = _dim(f"   · {prog[-1].lstrip('• ')[:46]}") if prog else ""
            print(f"  {idtag}{who} {_gray(t['to_whom'])}: {t['body'][:46]}{last}")
    if not closed:
        n = sum(1 for t in store.list_tasks() if t["status"] in _CLOSED)
        if n:
            print(_gray(f"  ({n} closed — /tasks closed · /task <id> for detail)"))


def _task_detail(store, tid: int) -> None:
    try:
        t = store.get_task(tid)
    except AssertionError:
        print(_red(f"  no task #{tid}"))
        return
    who = _gray(f" @{t['assignee']}") if t.get("assignee") else ""
    print("  " + _b(f"#{t['id']}") + " " + _task_c(t["status"], f"[{t['status']}]")
          + f" {t.get('kind') or 'task'} {_gray('→ ' + t['to_whom'])}" + who)
    print("  " + t["body"])
    if t.get("result"):
        print("  " + _grn("result: ") + t["result"])
    print(_gray("  timeline:"))
    for c in store.task_comments(tid):
        ts = time.strftime("%H:%M", time.localtime(c["ts"]))
        lines = (c["body"] or "").splitlines() or [""]
        print(_gray(f"    {ts} ") + _sender_c(c["author"]) + " "
              + _dim(f"[{c['kind']}]") + "  " + lines[0])
        for ln in lines[1:]:
            print("          " + ln)


def _is_tty() -> bool:
    import sys
    return sys.stdout.isatty()


def _pt_author_style(author: str) -> str:
    a = (author or "").lower()
    if a == "manager":
        return "class:manager"
    if a == "decmux":
        return "class:decmux"
    if a in ("human", "you", "user", "me"):
        return "class:human"
    return "class:agent"


def _task_browser(st: AppState) -> None:
    """Full-screen task browser: ↑/↓ move, the detail pane tracks the selection live,
    Enter focuses that thread, q/Esc closes. Falls back to a printed list with no tty."""
    if not _is_tty():
        _tasks(st.store)                   # tests / pipes: no full-screen UI
        return
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.styles import Style

    store = st.store
    state = {"tasks": store.list_tasks(), "sel": 0}

    def reload():
        cur = state["tasks"][state["sel"]]["id"] if state["tasks"] else None
        state["tasks"] = store.list_tasks()
        ids = [t["id"] for t in state["tasks"]]
        state["sel"] = ids.index(cur) if cur in ids else 0

    def list_text():
        if not state["tasks"]:
            return [("class:dim", "  (no tasks yet)")]
        out = []
        for i, t in enumerate(state["tasks"]):
            sel = i == state["sel"]
            who = f" @{t['assignee']}" if t.get("assignee") else ""
            line = f"{'▶' if sel else ' '} #{t['id']} [{t['status']}]{who} {t['body'][:44]}"
            out.append(("class:sel" if sel else _pt_state_style(t["status"]), line + "\n"))
        return out

    def detail_text():
        if not state["tasks"]:
            return [("class:dim", "  no tasks")]
        t = state["tasks"][state["sel"]]
        out = [("class:hdr", f"#{t['id']} [{t['status']}]  {t.get('kind') or 'task'} → {t['to_whom']}")]
        if t.get("assignee"):
            out.append(("class:dim", f"   @{t['assignee']}"))
        out.append(("", f"\n\n{t['body']}\n"))
        if t.get("result"):
            out.append(("class:ok", f"\nresult: {t['result']}\n"))
        out.append(("class:dim", "\ntimeline:\n"))
        for c in store.task_comments(t["id"])[-16:]:
            ts = time.strftime("%H:%M", time.localtime(c["ts"]))
            out.append(("class:dim", f"  {ts} "))
            out.append((_pt_author_style(c["author"]), c["author"]))
            out.append(("class:dim", f" [{c['kind']}]  "))
            out.append(("", f"{c['body']}\n"))
        return out

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("c-p")
    def _(e):
        if state["tasks"]:
            state["sel"] = (state["sel"] - 1) % len(state["tasks"])

    @kb.add("down")
    @kb.add("c-n")
    def _(e):
        if state["tasks"]:
            state["sel"] = (state["sel"] + 1) % len(state["tasks"])

    @kb.add("r")
    def _(e):
        reload()

    @kb.add("enter")
    def _(e):
        e.app.exit(result=(state["tasks"][state["sel"]]["id"] if state["tasks"] else None))

    @kb.add("q")
    @kb.add("escape")
    @kb.add("c-c")
    def _(e):
        e.app.exit(result=None)

    layout = Layout(HSplit([
        Window(FormattedTextControl(list_text), height=Dimension(weight=1)),
        Window(height=1, char="─", style="class:dim"),
        Window(FormattedTextControl(detail_text), height=Dimension(weight=2), wrap_lines=True),
        Window(height=1, content=FormattedTextControl(
            lambda: [("class:dim", " ↑/↓ move · Enter focus thread · r reload · q close ")])),
    ]))
    style = Style.from_dict({
        "sel": "reverse bold", "hdr": "bold", "dim": "#888888", "ok": "#2ecc71",
        "open": "#3bb0c9", "in_progress": "#f1c40f", "triage": "#f1c40f",
        "manager": "#3bb0c9", "agent": "#c77dff", "human": "#2ecc71", "decmux": "#f1c40f",
    })
    st.browsing = True
    try:
        tid = Application(layout=layout, key_bindings=kb, full_screen=True, style=style).run()
    finally:
        st.browsing = False
    if tid is not None:
        st.thread = tid                    # Enter focuses the thread for follow-up chat
        _task_detail(store, tid)
        print(_gray(f"focused thread #{tid} — type to continue it"))


def _pt_state_style(status: str) -> str:
    return {"done": "class:ok", "answered": "class:ok", "open": "class:open",
            "in_progress": "class:in_progress", "triage": "class:triage"}.get(status, "")


def _feed(store, n: int) -> None:
    for c in store.recent_chat(kind="chat", limit=n):
        _render_message(c["frm"], c["body"], c["dst"])


def _report(store, n: int) -> None:
    for t in store.recent_transitions(n):
        print("  " + _state_c(t["from_state"], (t["from_state"] or "?")) + _gray(" → ")
              + _state_c(t["to_state"], (t["to_state"] or "?")) + "  "
              + _dim(bus._clean_name(t["title"] or "")))


def _usage(store) -> None:
    # headline: the usage-limit % scraped from the agent screen ("N% used"/"N% left")
    lp = store.latest_usage_pct()
    if lp:
        used = lp["used"]
        series = store.usage_pct_series(hours=5.0, buckets=40)
        print(_b("  5h usage limit") + _gray("   (scraped from the agent screen)"))
        print("  " + _color_pct(used, _spark_fixed(series)) + _gray("   oldest → now"))
        print("  " + _color_pct(used, f"{used:.0f}% used") + _gray(f"  ·  {100 - used:.0f}% left"))
        rate = store.usage_pct_rate(minutes=60.0)
        if rate is not None and rate > 0.1:
            print("  " + _yel(f"→ +{rate:.0f}%/h — at this rate, 100% in ~{(100 - used) / rate:.1f}h"))
        elif rate is not None:
            print(_gray("  → usage flat / not rising recently"))
        else:
            print(_gray("  → (collecting history to project the rate…)"))
        print(_gray("  reset time isn't on screen — check claude.ai for the window reset"))
        print()
    # secondary: decmux's own activity trend (turns / tool calls)
    counts = store.usage_series(hours=5.0, buckets=40)
    rate = store.usage_rate(minutes=30.0)
    w = store.usage_window(hours=5.0)
    print(_b("  activity — rolling 5h") + _gray("   (turns = agent turns, tools = tool calls)"))
    print("  " + _cyan(_spark(counts)) + _gray("   oldest → now"))
    print(_gray(f"  window so far: {w['turns']} turns · {w['tools']} tool calls"))
    if rate["turns_per_hr"] or rate["tools_per_hr"]:
        print(_gray(f"  recent rate (30m): {rate['turns_per_hr']:.0f} turns/hr · "
                    f"{rate['tools_per_hr']:.0f} tools/hr"))
    if not lp:
        print(_gray("  (no usage-% yet — appears once an agent screen shows 'N% used' / 'N% left')"))


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
            if rest == "closed":
                _tasks(st.store, closed=True)
            elif rest in ("list", "open"):
                _tasks(st.store, closed=False)
            else:
                _task_browser(st)          # interactive ↑/↓ browser (falls back to a list)
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
                print(_dim(f"→ new #{st.thread} (delivered {res.get('delivered', 0)}, "
                           f"queued {res.get('queued', 0)})"))
            else:
                print(_gray("new thread — your next message starts a fresh task"))
        elif cmd == "feed":
            _feed(st.store, _int(rest, 20))
        elif cmd == "report":
            _report(st.store, _int(rest, 20))
        elif cmd == "usage":
            _usage(st.store)
        elif cmd in ("goal", "loop"):
            # /goal IS the autonomous loop: set it and decmux drives the team toward
            # it (momentum nudges when idle; the toolbar shows the next wakeup).
            if not rest:
                g = st.store.get_goal()
                if g:
                    print(_b("goal: ") + g)
                    print(_gray("  decmux is running the loop toward this — "
                                "toolbar shows the next wakeup. /goal <text> to change."))
                else:
                    print(_gray("usage: /goal <text>  — sets the goal and runs the "
                                "autonomous loop toward it"))
            else:
                res = bus.send(st.store, "/goal " + rest, to="manager", frm="human")
                print(_grn("loop running") + _gray(f" toward: {rest}  "
                      f"(delivered {res.get('delivered', 0)}, queued {res.get('queued', 0)})"))
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
        print(_dim(f"→ #{st.thread} (delivered {res['delivered']}, queued {res['queued']})"))
        return True
    res = bus.send(st.store, line, to=st.target, frm="human")
    if res.get("withheld_status"):
        print(_yel("withheld (status-only downward); use the agent's name or --force"))
        return True
    if st.target == "manager" and res.get("task"):
        st.thread = res["task"]            # a fresh message to the manager opens a thread
    tid = f" #{res['task']}" if res.get("task") else ""
    extra = " [gated→manager]" if res.get("gated_to_manager") else ""
    print(_dim(f"→ {res['dst']}{tid} (delivered {res['delivered']}, "
               f"queued {res.get('queued', 0)}){extra}"))
    return True


def _feed_poller(st: AppState) -> None:
    """Tail the store; print new manager->you messages and alert transitions.

    Uses its own Store connection (sqlite connections are per-thread)."""
    store = Store(st.workspace_uuid)
    last_chat = store.last_chat_id()
    last_tr = store.last_transition_id()
    while st.running:
        if st.browsing:                    # the full-screen browser owns the screen
            time.sleep(0.3)
            continue
        try:
            for c in store.chat_after(last_chat, kind="chat"):
                last_chat = c["id"]
                if c["frm"] != "human":           # incoming, not our own echo
                    _render_message(c["frm"], c["body"])
            for t in store.transitions_after(last_tr):
                last_tr = t["id"]
                g = _GLYPH.get(t["to_state"], "·")
                print(_state_c(t["to_state"],
                               f"{g} {bus._clean_name(t['title'] or '')} → {t['to_state']}"))
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


def _maybe_update() -> None:
    """On startup: if GitHub has a newer version, ask to update (y/N). Skipped for a
    dev/editable checkout and non-interactive runs. Runs before the prompt-toolkit
    session, so a plain input() prompt is safe here."""
    from . import update as _update
    if not _is_tty() or _update.is_editable():
        return
    info = _update.update_available(timeout=2.5)
    if not info:
        return
    cur, latest = info
    try:
        ans = input(_yel(f"decmux {latest} available (you have {cur}). update now? [y/N] "))
    except (EOFError, KeyboardInterrupt):
        return
    if ans.strip().lower() in ("y", "yes"):
        print(_gray("updating…"))
        if _update.run_install(latest):
            print(_grn(f"updated to {latest} — restart decmux (/quit then `decmux`)"))
        else:
            print(_red("update failed — try `decmux update` to see the error"))


def _wakeup_label(store) -> str:
    """`next: <what> Nm (HH:MM)` — when the supervision loop next acts proactively."""
    raw = store.get_meta("next_wakeup_ts", "")
    if not raw:
        return ""
    ts = float(raw)
    mins = max(0, round((ts - time.time()) / 60))
    kind = (store.get_meta("next_wakeup_kind", "") or "wake")[:16]
    return f"  next:{kind} {mins}m ({time.strftime('%H:%M', time.localtime(ts))})"


def _toolbar(st: AppState) -> str:
    counts = Counter(a["state"] for a in st.store.list_agents())
    parts = "  ".join(f"{_GLYPH.get(s, '·')}{n}" for s, n in counts.items()) or "no agents"
    goal = st.store.get_goal()
    tail = f"  goal: {goal[:28]}" if goal else ""
    return (f" decmux  {parts}  open:{len(st.store.open_tasks())}"
            f"  ->{st.target}{_wakeup_label(st.store)}{tail} ")


def repl(workspace_uuid: str, *, notify: bool = True) -> int:
    import shutil as _shutil
    import sys as _sys

    _maybe_update()                     # offer an update before the TUI starts (y/N)

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
    psession: PromptSession = PromptSession(multiline=True, key_bindings=kb, style=style,
                                            refresh_interval=15)  # tick the toolbar countdown
    _sys.stdout.write("\x1b[>4;1m")     # ask the terminal to report Shift+Enter (modifyOtherKeys L1)
    _sys.stdout.flush()
    print(f"decmux — workspace {workspace_uuid}. supervising in the background.")
    print("Enter sends · Shift+Enter / Alt+Enter for a newline · /help · /quit")
    _startup_guide(st.store)
    try:
        # raw=True: keep our SGR color codes intact. The default StdoutProxy routes
        # background/print output through Output.write(), which sanitizes ESC to '?'
        # (the "messages show up garbled / ?[2m" bug) — write_raw preserves them.
        with patch_stdout(raw=True):
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

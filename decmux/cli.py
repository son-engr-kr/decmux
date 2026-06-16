"""decmux CLI — the per-workspace entry point and the verbs agents/scripts call.

`decmux` with no args opens the current workspace's session (foreground
supervision). Every verb resolves the caller's workspace via `cmux identify` and
operates on that workspace's store, so there is nothing global to configure.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

from . import app, assets, bus, cmux, hooks, session, watch
from . import store as store_mod
from .store import Store


# --- workspace resolution (the caller's cmux workspace) ---
def _caller() -> dict:
    return cmux.run_json("identify", "--id-format", "both", "--json")["caller"]


def _ws_uuid(args: argparse.Namespace | None = None) -> str:
    if args is not None and getattr(args, "workspace", None):
        return args.workspace
    return _caller()["workspace_id"]


def _store(args: argparse.Namespace | None = None) -> Store:
    return Store(_ws_uuid(args))


# --- status rendering ---
def _row_to_dict(r: watch.Row, kinds: dict | None = None) -> dict:
    return {
        "surface": r.surface.ref, "uuid": r.surface.uuid,
        "name": bus._clean_name(r.surface.title), "state": r.state,
        "kind": (kinds or {}).get(r.surface.uuid, ""),
        "busy_kind": r.busy_kind, "model": r.model, "effort": r.effort,
        "cwd": r.workspace_cwd, "quiet_for": round(r.quiet_for or 0.0, 1),
    }


_GLYPH = {"working": "●", "idle": "○", "stuck": "▲", "error": "✖",
          "dead": "☠", "budget": "$", "blocked-on-decision": "?"}


def _render(rows: list[watch.Row], kinds: dict | None = None) -> str:
    if not rows:
        return "(no agents in this workspace)"
    kinds = kinds or {}
    out = []
    for r in rows:
        g = _GLYPH.get(r.state, "·")
        bk = f"·{r.busy_kind}" if r.state == "working" and r.busy_kind else ""
        k = kinds.get(r.surface.uuid, "")
        eff = f" [{r.effort}]" if r.effort else ""
        out.append(f"  {g} {r.state + bk:16} {bus._clean_name(r.surface.title):18} "
                   f"{r.surface.ref:11} {k:7} {r.model or ''}{eff}")
    return "\n".join(out)


def cmd_status(args: argparse.Namespace) -> int:
    store = _store(args)
    watcher = watch.Watcher(store.workspace_uuid, detect_errors=False)  # one-shot: fast

    def poll() -> list[watch.Row]:
        rows = watcher.poll()
        if not args.all:                    # default: only surfaces decmux manages
            managed = store.managed_set()
            rows = [r for r in rows if r.surface.uuid in managed]
        bk = store.busy_kind_by_surface()   # overlay the session's last-known shell/llm
        for r in rows:
            if not r.busy_kind:
                r.busy_kind = bk.get(r.surface.uuid, "") or ""
        return rows

    kinds = store.managed_kinds()

    def header(rows: list[watch.Row]) -> str:
        cwd = (rows[0].workspace_cwd if rows else "") or store.get_meta("cwd")
        u = store.usage()
        parts = []
        if cwd:
            parts.append(f"dir {cwd}")
        if u.get("turns") or u.get("tools"):
            parts.append(f"usage {u.get('turns') or 0} turns / {u.get('tools') or 0} tools")
        return "   ·   ".join(parts)

    if args.json:
        print(json.dumps([_row_to_dict(r, kinds) for r in poll()], indent=2, ensure_ascii=False))
        return 0
    if args.watch:
        try:
            while True:
                rows = poll()
                sys.stdout.write("\033[2J\033[H")
                print(f"decmux status — {time.strftime('%H:%M:%S')} (ctrl-c to stop)")
                print(header(rows) + "\n")
                print(_render(rows, kinds))
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
    rows = poll()
    h = header(rows)
    if h:
        print(h)
    print(_render(rows, kinds))
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    c = _caller()
    print(f"workspace {c.get('workspace_ref')} ({c.get('workspace_id')})")
    print(f"surface   {c.get('surface_ref')} ({c.get('surface_id')})  type={c.get('surface_type')}")
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    """Bind the caller's surface as this workspace's manager (idempotent)."""
    c = _caller()
    store = Store(c["workspace_id"])
    cwd = next((w.get("current_directory", "")
                for w in cmux.run_json("workspace", "list", "--json").get("workspaces", [])
                if w["ref"] == c["workspace_ref"]), "")
    old = store.manager()
    store.bind_manager(surface_uuid=c["surface_id"], surface_ref=c["surface_ref"], cwd=cwd)
    if old and old != (c["surface_id"], c["surface_ref"]):
        store.reassign_manager_work(
            old_surface_uuid=old[0], old_surface_ref=old[1],
            new_surface_uuid=c["surface_id"], new_surface_ref=c["surface_ref"])
        bus.deliver_manager_backlog(
            store, reason="Manager was changed; open manager tasks were requeued.")
    store.mark_managed(c["surface_id"], role="manager")
    store.commit()
    print(f"registered manager: {c['surface_ref']} @ {c['workspace_ref']}")
    return 0


def cmd_goal(args: argparse.Namespace) -> int:
    """Set this workspace's goal and deliver it to the manager as operating context."""
    store = _store(args)
    text = " ".join(args.text).strip()
    assert text, "goal text required"
    store.set_goal(text)
    res = bus.deliver_goal_update(store, text, author=bus.resolve_sender(store))
    store.commit()
    print(f"goal set (delivered {res['delivered']}, queued {res['queued']})")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    """Message an agent / manager / you through decmux: record it + deliver/queue."""
    res = bus.send(_store(args), " ".join(args.text), to=args.to, frm=args.frm,
                   force=args.force)
    if res.get("withheld_status"):
        print("withheld status-only message — downward messages should be commands; "
              "logged to the timeline. Resend with --force if it was a command.")
        return 0
    closed = f", closed task #{res['closed_task']}" if res.get("closed_task") else ""
    gated = (f", gated from {res.get('requested_dst')} to manager"
             if res.get("gated_to_manager") else "")
    print(f"{res['frm']} -> {res['dst']} (delivered {res['delivered']}, "
          f"queued {res.get('queued', 0)}{closed}{gated})")
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.action == "list":
        rows = store.list_tasks(include_comments=args.comments)
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return 0
        for t in rows:
            tail = f"  => {t['result'][:60]}" if t["result"] else ""
            print(f"#{t['id']} [{t['status']}] {t['kind']} -> {t['to_whom']}: "
                  f"{t['body'][:80]}{tail}")
            if args.comments:
                for c in t.get("comments", []):
                    print(f"    - {c['author']} [{c['kind']}]: {c['body'][:120]}")
        return 0
    if args.action == "add":
        parts = ([args.id] if args.id else []) + args.text
        assert parts, "text required for task add"
        text = " ".join(parts)
        kind = args.kind or ("question" if text.strip().endswith("?") else "command")
        tid = store.add_task(kind=kind, body=text, to_whom=args.to,
                             source=args.source, author=args.author)
        store.add_chat(frm=args.author, dst=args.to, body=text,
                       kind=("chat" if args.author in bus._HUMAN else "report"))
        store.commit()
        delivered = bus.deliver_task(store, store.get_task(tid))
        queued = store.task_pending_delivery_count(tid)
        store.commit()
        print(f"task #{tid} open (delivered {delivered}, queued {queued})")
        return 0
    assert args.id, f"task id required for {args.action}"
    tid = int(args.id)
    task = store.get_task(tid)
    sender = bus.resolve_sender(store)
    if args.action == "wait":
        deadline = time.time() + args.timeout if args.timeout else None
        while task["status"] not in ("done", "answered"):
            if deadline and time.time() >= deadline:
                print(f"task #{tid} still {task['status']}")
                return 1
            time.sleep(args.interval)
            task = store.get_task(tid)
        print(f"task #{tid} {task['status']}: {task['result']}")
        return 0
    if args.action == "show":   # the pull target for a digest pointer: full thread
        if args.json:
            print(json.dumps({**task, "comments": store.task_comments(tid)},
                             indent=2, ensure_ascii=False))
            return 0
        print(f"#{task['id']} [{task['status']}] {task['kind']} -> {task['to_whom']}"
              + (f"  (assignee: {task['assignee']})" if task.get("assignee") else ""))
        print(f"  request: {task['body']}")
        if task.get("result"):
            print(f"  result:  {task['result']}")
        print("  timeline:")
        for c in store.task_comments(tid):
            print(f"    {time.strftime('%H:%M:%S', time.localtime(c['ts']))} "
                  f"{c['author']} [{c['kind']}]: {c['body']}")
        return 0
    assert args.text or args.action == "reopen", f"text required for task {args.action}"
    text = " ".join(args.text)
    if args.action in ("comment", "progress"):   # 'progress' is an alias for comment
        store.add_task_comment(tid, author=sender, kind="comment", body=text)
        res = bus.deliver_task_update(store, store.get_task(tid), kind="comment",
                                      body=text, author=sender)
        body = f"task #{tid} comment: {text} (delivered {res['delivered']}, queued {res['queued']})"
    elif args.action == "claim":
        bus.assert_manager_workflow(task, action="claim", author=sender)
        store.claim_task(tid, text)
        body = f"task #{tid} claimed by {text}"
    elif args.action == "delegate":
        assert len(args.text) >= 2, "delegate requires: <agent> <instruction>"
        res = bus.delegate_task(store, tid, args.text[0], " ".join(args.text[1:]), author=sender)
        body = (f"task #{tid} delegated to {res['assignee']} "
                f"(delivered {res['delivered']}, queued {res['queued']})")
    elif args.action in ("done", "answer"):
        status = "done" if args.action == "done" else "answered"
        store.close_task(tid, text, status, author=sender)
        # a subordinate closing a task reports up to the manager as a lean digest
        # pointer; the full result stays in the task thread (pull: decmux task <id>).
        res = (bus.deliver_task_update(store, store.get_task(tid), kind=status,
                                       body=text, author=sender)
               if bus._is_report_up(sender) else None)
        extra = f" (queued {res['queued']})" if res and res.get("queued") else ""
        body = f"task #{tid} {'done' if status == 'done' else 'answer'}: {text}{extra}"
    elif args.action == "reopen":
        store.reopen_task(tid, author=sender)
        res = bus.deliver_task_update(store, store.get_task(tid), kind="reopened",
                                      body=(text or "task reopened"), author=sender)
        body = f"task #{tid} reopened (delivered {res['delivered']}, queued {res['queued']})"
    store.add_chat(frm=sender, dst="manager", body=body, kind="report")
    if (task.get("author") or "").strip().lower() in bus._HUMAN:
        # the asker gets answers/updates back in their REPL feed (human-facing chat)
        store.add_chat(frm=sender, dst="human", body=body, kind="chat")
    store.commit()
    print(f"task #{tid} {args.action}")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    store = _store(args)
    r = store.db.execute(
        "SELECT surface_uuid FROM agent_state WHERE surface_ref=? OR title=? OR title=?",
        (args.agent, args.agent, "✳ " + args.agent),
    ).fetchone()
    assert r, f"agent {args.agent} not found"
    store.set_note(r["surface_uuid"], args.text)
    store.commit()
    print("note set")
    return 0


def cmd_decisions(args: argparse.Namespace) -> int:
    rows = _store(args).list_decisions(args.status)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        print(f"{(r['request_id'] or '')[:28]:28}  [{r['status']}/{r['disposition']}]  "
              f"{r['hook_event']} {r['tool_name']}")
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.kind == "question":
        cmux.feed_reply_question(args.request_id, args.value.split(","))
    else:
        cmux.feed_reply_permission(args.request_id, args.value)
    store.resolve_decision(args.request_id, "answered")
    store.commit()
    print(f"answered {args.request_id}: {args.value}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    store = _store(args)
    print("recent transitions:")
    for t in store.recent_transitions(args.limit):
        print(f"  {time.strftime('%H:%M:%S', time.localtime(t['ts']))} "
              f"{t['from_state']} -> {t['to_state']}  {t['title']}")
    # operational traffic (reports up, delegations, pokes) — the detail behind a
    # digest pointer when it was a plain send rather than a tracked task.
    msgs = store.recent_chat(limit=args.limit, kind="report")
    if msgs:
        print("\nrecent messages:")
        for c in msgs:
            print(f"  {time.strftime('%H:%M:%S', time.localtime(c['ts']))} "
                  f"{c['frm']} -> {c['dst']}: {c['body'][:100]}")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    """List known workspaces (the only cross-workspace view)."""
    root = store_mod._root()
    if not root.exists():
        print("no workspaces yet")
        return 0
    names: dict[str, str] = {}
    try:
        names = {w["id"]: (w.get("title") or w["ref"])
                 for w in cmux.run_json("workspace", "list", "--id-format", "both",
                                        "--json").get("workspaces", [])}
    except (subprocess.CalledProcessError, OSError, KeyError):
        pass
    for d in sorted(p for p in root.iterdir() if (p / "store.db").exists()):
        s = Store(d.name)
        goal = s.get_goal()
        print(f"{names.get(d.name, d.name)[:30]:30}  agents={len(s.list_agents())} "
              f"open_tasks={len(s.open_tasks())}" + (f"  goal: {goal[:50]}" if goal else ""))
    return 0


def cmd_spawn(args: argparse.Namespace) -> int:
    """Create a new cmux surface and launch a decmux-managed agent in it."""
    store = _store(args)
    # provenance drives the reaper: a human-run spawn is never auto-closed.
    origin = "human" if bus.resolve_sender(store) in bus._HUMAN else "self"
    res = bus.spawn_agent(store, name=args.name, kind=args.kind, manager=args.manager,
                          command=args.command, term=args.term, origin=origin,
                          worktree=args.worktree, branch=args.branch)
    if res.get("created"):
        tags = [] if res["manager"] else [res.get("term", "short")]
        if res.get("worktree"):
            tags.append(f"worktree {res['worktree']}")
        tag = f"  [{', '.join(tags)}]" if tags else ""
        print(f"spawned {res['name']}: {res['surface_ref']}"
              + (" (manager)" if res["manager"] else "") + tag)
    else:
        print(res.get("reason", "not created"))
    return 0


def cmd_despawn(args: argparse.Namespace) -> int:
    """Release an agent: graceful by default (wrap up, hand off, then closed when
    idle); --now archives and closes immediately."""
    res = bus.despawn(_store(args), args.agent, now=args.now)
    if res["closed"]:
        print(f"despawned {res['name']} — surface closed; transcript: {res['archive']}")
    else:
        print(f"releasing {res['name']} — it will be archived and closed once idle and handed off")
    return 0


def _agent_launch(*, caller: dict, role: str, kind: str | None, command: str | None,
                  guard_dir: str, real_cmux: str | None) -> tuple[list[str], dict]:
    """Build (argv, env) to exec an agent in the current surface, decmux-tagged."""
    env = dict(os.environ)
    env.update({
        "DECMUX_ROLE": role,
        "CMUX_WORKSPACE_ID": caller.get("workspace_id", ""),
        "CMUX_WORKSPACE_REF": caller.get("workspace_ref", ""),
        "CMUX_SURFACE_ID": caller.get("surface_id", ""),
        "CMUX_SURFACE_REF": caller.get("surface_ref", ""),
        "DECMUX_REAL_CMUX": real_cmux or "",
        "PATH": f"{guard_dir}:{env.get('PATH', '')}",
    })
    cmd = command or bus.AGENT_CMD.get(kind or "claude", bus.AGENT_CMD["claude"])
    return shlex.split(cmd), env


def cmd_agent(args: argparse.Namespace) -> int:
    """Become a decmux-managed agent in THIS surface (run instead of `claude`).

    Sets DECMUX_ROLE + the cmux-send guard and execs the agent in place, so its
    SessionStart hook injects the decmux protocol — the way to onboard a surface
    you opened yourself, without spawning a new one."""
    c = _caller()
    store = Store(c["workspace_id"])
    role = "manager" if args.manager else "agent"
    if args.manager:
        cwd = next((w.get("current_directory", "")
                    for w in cmux.run_json("workspace", "list", "--json").get("workspaces", [])
                    if w["ref"] == c["workspace_ref"]), "")
        store.bind_manager(surface_uuid=c["surface_id"], surface_ref=c["surface_ref"], cwd=cwd)
        store.commit()
    if args.name:
        cmux.run("rename-tab", "--workspace", c["workspace_ref"], "--surface", c["surface_ref"], args.name)
    store.mark_managed(c["surface_id"], role=role, kind=args.kind)
    if args.kind == "codex":          # claude gets the protocol via the SessionStart hook
        bus.deliver_protocol(store, c["surface_id"], c["surface_ref"])
    store.commit()
    guard_dir = assets._ensure_cmux_guard()
    argv, env = _agent_launch(caller=c, role=role, kind=args.kind, command=args.command,
                              guard_dir=str(guard_dir), real_cmux=cmux.CMUX_BIN)
    os.execvpe(argv[0], argv, env)   # replaces this process with the agent


def cmd_session_start(args: argparse.Namespace) -> int:
    """Claude Code SessionStart hook entry: refresh + reload the decmux skill."""
    return hooks.session_start()


def cmd_purge(args: argparse.Namespace) -> int:
    """Delete decmux's stored data (this workspace by default, or --all).

    decmux only ever deletes data; removing the tool itself + its Claude hook is
    `uv tool uninstall decmux` (the SessionStart hook self-guards to a no-op)."""
    root = store_mod._root()
    if args.all:
        if root.exists():
            shutil.rmtree(root)
            print(f"removed ALL decmux data: {root}")
        else:
            print(f"no decmux data to remove ({root})")
        return 0
    ws = _ws_uuid(args)
    d = root / ws
    if d.exists():
        shutil.rmtree(d)
        print(f"removed data for this workspace: {d}")
    else:
        print(f"no data for this workspace ({d})")
    return 0


def _setup_hint() -> None:
    """Nudge toward explicit setup, without writing global config on every run."""
    if not hooks.claude_status()["session_start_hook"]:
        print("tip: run `decmux setup` once so agents learn the protocol "
              "(installs a Claude SessionStart hook).")


def cmd_teardown(args: argparse.Namespace) -> int:
    """Remove decmux entirely: global hook + cmux guard + ALL data + the command."""
    root = store_mod._root()
    print("decmux teardown will remove:")
    print("  • the global Claude SessionStart hook (~/.claude/settings.json)")
    print(f"  • the cmux guard ({assets.GUARD_DIR})")
    print(f"  • ALL workspace data ({root})")
    print("  • the decmux command (uv tool uninstall decmux)")
    if not args.yes:
        if input("proceed? [y/N] ").strip().lower() != "y":
            print("aborted")
            return 0
    hooks.remove_session_hook()
    if assets.GUARD_DIR.exists():
        shutil.rmtree(assets.GUARD_DIR)
    if root.exists():
        shutil.rmtree(root)
    print("removed hook + guard + data; uninstalling the command…")
    try:
        subprocess.run(["uv", "tool", "uninstall", "decmux"], check=False)
    except (FileNotFoundError, OSError) as e:
        print(f"  could not run uv ({e}); finish with: uv tool uninstall decmux")
    print("decmux removed.")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Install (or --remove) the global Claude SessionStart hook that injects the
    decmux protocol into decmux-managed sessions (no skill file). The cmux guard is
    created on demand when you `decmux spawn`/`agent`."""
    if args.remove:
        removed = hooks.remove_session_hook()
        print("removed the global decmux SessionStart hook" if removed
              else "no decmux hook installed")
        return 0
    res = hooks.install_all_hooks()
    print("decmux setup complete:")
    print(f"  Claude SessionStart hook (~/.claude/settings.json): "
          f"{'installed' if res['session_hook'] else 'already present'}")
    print("  protocol is injected per-session for decmux surfaces only (no skill file)")
    print("\nundo:  decmux setup --remove   (hook)   ·   decmux purge   (data)   ·   "
          "uv tool uninstall decmux   (command)")
    return 0


def cmd_app(args: argparse.Namespace) -> int:
    """The no-arg entry: open the interactive REPL (chat + commands), supervising
    in the background. Does not touch global config — run `decmux setup` for that."""
    _setup_hint()
    return app.repl(_ws_uuid(args), notify=not args.no_notify)


def cmd_run(args: argparse.Namespace) -> int:
    """Headless foreground supervision (no REPL) — `decmux run`."""
    _setup_hint()
    sess = session.Session(_ws_uuid(args), notify=not args.no_notify, pin=args.pin)
    print(f"decmux: supervising workspace {sess.workspace_uuid} (ctrl-c to stop)")
    return sess.run(interval=args.interval, ticks=args.ticks)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="decmux", description="per-workspace control plane for cmux agents")
    p.set_defaults(func=cmd_app, interval=5.0, ticks=0, pin=False, no_notify=False, workspace=None)
    sub = p.add_subparsers(dest="command")

    pr = sub.add_parser("run", help="run foreground supervision for this workspace")
    pr.add_argument("--interval", type=float, default=5.0)
    pr.add_argument("--ticks", type=int, default=0)
    pr.add_argument("--pin", action="store_true", help="write a cmux status pill")
    pr.add_argument("--no-notify", action="store_true")
    pr.add_argument("--workspace")
    pr.set_defaults(func=cmd_run)

    ps = sub.add_parser("status", help="classify and show this workspace's agents")
    ps.add_argument("--json", action="store_true")
    ps.add_argument("--watch", action="store_true")
    ps.add_argument("--all", action="store_true", help="include surfaces decmux isn't managing")
    ps.add_argument("--interval", type=float, default=2.0)
    ps.add_argument("--workspace")
    ps.set_defaults(func=cmd_status)

    sub.add_parser("whoami", help="show the caller's workspace/surface").set_defaults(func=cmd_whoami)
    sub.add_parser("ls", help="list known workspaces").set_defaults(func=cmd_ls)

    pg = sub.add_parser("register", help="bind the caller surface as this workspace's manager")
    pg.set_defaults(func=cmd_register)

    pgo = sub.add_parser("goal", help="set the workspace goal and brief the manager")
    pgo.add_argument("text", nargs="+")
    pgo.add_argument("--workspace")
    pgo.set_defaults(func=cmd_goal)

    psend = sub.add_parser("send", help="message manager/human/<agent>/all through decmux")
    psend.add_argument("text", nargs="+")
    psend.add_argument("--to", default="manager")
    psend.add_argument("--frm", default=None)
    psend.add_argument("--force", action="store_true")
    psend.add_argument("--workspace")
    psend.set_defaults(func=cmd_send)

    pt = sub.add_parser("task", help="task queue (tracked like issues)")
    pt.add_argument("action", choices=["list", "show", "add", "comment", "progress", "claim",
                                       "delegate", "done", "answer", "reopen", "wait"])
    pt.add_argument("id", nargs="?")
    pt.add_argument("text", nargs="*")
    pt.add_argument("--to", default="manager")
    pt.add_argument("--author", default="human")
    pt.add_argument("--source", default="chat")
    pt.add_argument("--kind")
    pt.add_argument("--json", action="store_true")
    pt.add_argument("--comments", action="store_true")
    pt.add_argument("--timeout", type=float, default=0.0)
    pt.add_argument("--interval", type=float, default=2.0)
    pt.add_argument("--workspace")
    pt.set_defaults(func=cmd_task)

    pn = sub.add_parser("note", help="set an agent's summary/why-idle note")
    pn.add_argument("agent")
    pn.add_argument("text")
    pn.add_argument("--workspace")
    pn.set_defaults(func=cmd_note)

    pd = sub.add_parser("decisions", help="list Feed decisions + disposition")
    pd.add_argument("--status")
    pd.add_argument("--json", action="store_true")
    pd.add_argument("--workspace")
    pd.set_defaults(func=cmd_decisions)

    pan = sub.add_parser("answer", help="answer a Feed decision via cmux rpc")
    pan.add_argument("request_id")
    pan.add_argument("value")
    pan.add_argument("--kind", choices=["permission", "question"], default="permission")
    pan.add_argument("--workspace")
    pan.set_defaults(func=cmd_answer)

    prep = sub.add_parser("report", help="recent state transitions")
    prep.add_argument("--limit", type=int, default=30)
    prep.add_argument("--workspace")
    prep.set_defaults(func=cmd_report)

    pag = sub.add_parser("agent", help="become a decmux agent in THIS surface (run instead of claude)")
    pag.add_argument("--name")
    pag.add_argument("--kind", choices=["claude", "codex"], default="claude")
    pag.add_argument("--manager", action="store_true")
    pag.add_argument("--command")
    pag.set_defaults(func=cmd_agent)

    psp = sub.add_parser("spawn", help="create a new agent in its own surface")
    psp.add_argument("--name")
    psp.add_argument("--kind", choices=["claude", "codex"], default="claude")
    psp.add_argument("--manager", action="store_true")
    psp.add_argument("--command")
    psp.add_argument("--term", choices=["short", "long", "full"], default="short",
                     help="employment term: short=per-task, long=per-workstream, full=permanent")
    psp.add_argument("--worktree", action="store_true",
                     help="run the agent in a fresh git worktree (parallel/exploratory work)")
    psp.add_argument("--branch", help="branch name for the worktree")
    psp.add_argument("--workspace")
    psp.set_defaults(func=cmd_spawn)

    pds = sub.add_parser("despawn", help="release an agent (graceful; --now closes immediately)")
    pds.add_argument("agent")
    pds.add_argument("--now", action="store_true", help="archive and close right away")
    pds.add_argument("--workspace")
    pds.set_defaults(func=cmd_despawn)

    psu = sub.add_parser("setup", help="install (or --remove) the global Claude SessionStart hook")
    psu.add_argument("--remove", action="store_true", help="uninstall the global hook (inverse of setup)")
    psu.set_defaults(func=cmd_setup)

    sub.add_parser("session-start", help="(internal) Claude SessionStart hook entry") \
        .set_defaults(func=cmd_session_start)

    pp = sub.add_parser("purge", help="delete decmux's stored data (this workspace, or --all)")
    pp.add_argument("--all", action="store_true", help="delete data for every workspace")
    pp.add_argument("--workspace")
    pp.set_defaults(func=cmd_purge)

    ptd = sub.add_parser("teardown", help="remove decmux entirely: hook + guard + ALL data + the command")
    ptd.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ptd.set_defaults(func=cmd_teardown)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

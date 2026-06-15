"""decmux CLI — the per-workspace entry point and the verbs agents/scripts call.

`decmux` with no args opens the current workspace's session (foreground
supervision). Every verb resolves the caller's workspace via `cmux identify` and
operates on that workspace's store, so there is nothing global to configure.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time

from . import app, assets, bus, cmux, hooks, session, watch
from . import store as store_mod
from .store import Store

AGENT_CMD = {"claude": "claude --dangerously-skip-permissions", "codex": "codex --yolo"}


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
def _row_to_dict(r: watch.Row) -> dict:
    return {
        "surface": r.surface.ref, "uuid": r.surface.uuid,
        "name": bus._clean_name(r.surface.title), "state": r.state,
        "busy_kind": r.busy_kind, "model": r.model, "effort": r.effort,
        "quiet_for": round(r.quiet_for or 0.0, 1),
    }


_GLYPH = {"working": "●", "idle": "○", "stuck": "▲", "error": "✖",
          "dead": "☠", "budget": "$", "blocked-on-decision": "?"}


def _render(rows: list[watch.Row]) -> str:
    if not rows:
        return "(no agents in this workspace)"
    out = []
    for r in rows:
        g = _GLYPH.get(r.state, "·")
        kind = f"·{r.busy_kind}" if r.state == "working" and r.busy_kind else ""
        model = f"  {r.model}" if r.model else ""
        out.append(f"  {g} {r.state + kind:18} {bus._clean_name(r.surface.title):24}"
                   f" {r.surface.ref:11}{model}")
    return "\n".join(out)


def cmd_status(args: argparse.Namespace) -> int:
    store = _store(args)
    watcher = watch.Watcher(store.workspace_uuid, detect_errors=False)  # one-shot: fast

    def poll() -> list[watch.Row]:
        rows = watcher.poll()
        bk = store.busy_kind_by_surface()   # overlay the session's last-known shell/llm
        for r in rows:
            if not r.busy_kind:
                r.busy_kind = bk.get(r.surface.uuid, "") or ""
        return rows

    if args.json:
        print(json.dumps([_row_to_dict(r) for r in poll()], indent=2, ensure_ascii=False))
        return 0
    if args.watch:
        try:
            while True:
                sys.stdout.write("\033[2J\033[H")
                print(f"decmux status — {time.strftime('%H:%M:%S')} (ctrl-c to stop)\n")
                print(_render(poll()))
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
    print(_render(poll()))
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
                       kind=("chat" if args.author == "you" else "report"))
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
    elif args.action == "done":
        store.close_task(tid, text, "done", author=sender)
        body = f"task #{tid} done: {text}"
    elif args.action == "answer":
        store.close_task(tid, text, "answered", author=sender)
        body = f"task #{tid} answer: {text}"
    elif args.action == "reopen":
        store.reopen_task(tid, author=sender)
        res = bus.deliver_task_update(store, store.get_task(tid), kind="reopened",
                                      body=(text or "task reopened"), author=sender)
        body = f"task #{tid} reopened (delivered {res['delivered']}, queued {res['queued']})"
    store.add_chat(frm=sender, dst="manager", body=body, kind="report")
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


def cmd_apply(args: argparse.Namespace) -> int:
    print(bus.apply_skill(_store(args), force=args.force))
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
    """Create a new agent in its own named cmux surface (optionally the manager)."""
    ws_uuid = _ws_uuid(args)
    wl = cmux.run_json("workspace", "list", "--id-format", "both", "--json")["workspaces"]
    w = next((x for x in wl if x.get("id") == ws_uuid), None)
    assert w, f"workspace {ws_uuid} not found"
    ws_ref, cwd = w["ref"], w.get("current_directory", "")
    store = Store(ws_uuid)
    if args.manager and store.manager():
        print(f"manager already bound for {ws_ref} (no-op)")
        return 0
    out = cmux.run("new-surface", "--type", "terminal", "--workspace", ws_ref,
                   "--no-focus", "--id-format", "both")
    m = re.search(r"(surface:\d+)\s+\(([0-9A-Fa-f-]+)\)", out)
    assert m, f"could not parse new surface id from: {out!r}"
    sref, suuid = m.group(1), m.group(2)
    name = args.name or ("manager" if args.manager else "agent")
    cmux.run("rename-tab", "--workspace", ws_ref, "--surface", sref, name)
    cmd = args.command or AGENT_CMD.get(args.kind or "claude", AGENT_CMD["claude"])
    cmd = assets.guarded_command(cmd, env={
        "CMUX_WORKSPACE_ID": ws_uuid, "CMUX_WORKSPACE_REF": ws_ref,
        "CMUX_SURFACE_ID": suuid, "CMUX_SURFACE_REF": sref,
        "DECMUX_ROLE": "manager" if args.manager else "agent",
    }, cwd=cwd or None)
    cmux.run("send", "--workspace", ws_ref, "--surface", sref, cmd)
    cmux.run("send-key", "--workspace", ws_ref, "--surface", sref, "Enter")
    if args.manager:
        store.bind_manager(surface_uuid=suuid, surface_ref=sref, cwd=cwd)
        store.commit()
    print(f"spawned {name}: {sref} @ {ws_ref}" + (" (manager)" if args.manager else ""))
    return 0


def cmd_hooks(args: argparse.Namespace) -> int:
    if args.action == "install":
        print(json.dumps(hooks.install_all_hooks(), indent=2))
    else:
        print(json.dumps(hooks.claude_status(), indent=2))
    return 0


def cmd_session_start(args: argparse.Namespace) -> int:
    """Claude Code SessionStart hook entry: refresh + reload the decmux skill."""
    return hooks.session_start()


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove decmux's installed integration (skill + cmux guard + Claude hook).

    Keeps your per-workspace data by default; pass --data to wipe that too. This
    does not remove the `decmux` command itself — run `uv tool uninstall decmux`."""
    a = assets.remove()
    h = hooks.remove_hooks()
    print("removed decmux integration:")
    print(f"  skill (~/.claude/skills/decmux):        {'removed' if a['skill'] else 'absent'}")
    print(f"  cmux guard ({assets.GUARD_DIR}): {'removed' if a['guard'] else 'absent'}")
    print(f"  Claude SessionStart hook:               {'removed' if h['session_removed'] else 'absent'}")
    if h["prompt_removed"]:
        print("  (also removed leftover legacy prompt hooks)")
    root = store_mod._root()
    if args.data:
        if root.exists():
            shutil.rmtree(root)
            print(f"\n  --data: ALSO removed all workspace data: {root}")
        else:
            print(f"\n  --data: no data to remove ({root})")
    else:
        print(f"\nkept your data: {root}")
        print("  (per-workspace tasks, chat, goals, agent state — `decmux uninstall --data` to wipe)")
    print("\nto remove the command itself:  uv tool uninstall decmux")
    return 0


def cmd_app(args: argparse.Namespace) -> int:
    """The no-arg entry: open the interactive REPL (chat + commands), supervising
    in the background."""
    assets.ensure()                 # zero-setup: skill + guard
    hooks.install_all_hooks()       # idempotent SessionStart hook
    return app.repl(_ws_uuid(args), notify=not args.no_notify)


def cmd_run(args: argparse.Namespace) -> int:
    """Headless foreground supervision (no REPL) — `decmux run`."""
    assets.ensure()
    hooks.install_all_hooks()
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

    psend = sub.add_parser("send", help="message manager/you/<agent>/all through decmux")
    psend.add_argument("text", nargs="+")
    psend.add_argument("--to", default="manager")
    psend.add_argument("--frm", default=None)
    psend.add_argument("--force", action="store_true")
    psend.add_argument("--workspace")
    psend.set_defaults(func=cmd_send)

    pt = sub.add_parser("task", help="task queue (tracked like issues)")
    pt.add_argument("action", choices=["list", "add", "comment", "progress", "claim",
                                       "delegate", "done", "answer", "reopen", "wait"])
    pt.add_argument("id", nargs="?")
    pt.add_argument("text", nargs="*")
    pt.add_argument("--to", default="manager")
    pt.add_argument("--author", default="you")
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

    pa = sub.add_parser("apply", help="onboard this workspace's agents to route through decmux")
    pa.add_argument("--force", action="store_true")
    pa.add_argument("--workspace")
    pa.set_defaults(func=cmd_apply)

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

    psp = sub.add_parser("spawn", help="create a new agent in its own surface")
    psp.add_argument("--name")
    psp.add_argument("--kind", choices=["claude", "codex"], default="claude")
    psp.add_argument("--manager", action="store_true")
    psp.add_argument("--command")
    psp.add_argument("--workspace")
    psp.set_defaults(func=cmd_spawn)

    ph = sub.add_parser("hooks", help="install/status the Claude Code SessionStart hook")
    ph.add_argument("action", choices=["install", "status"], nargs="?", default="status")
    ph.set_defaults(func=cmd_hooks)

    sub.add_parser("session-start", help="(Claude SessionStart hook) refresh + reload skill") \
        .set_defaults(func=cmd_session_start)

    pu = sub.add_parser("uninstall", help="remove the skill + guard + Claude hook (keeps your data)")
    pu.add_argument("--data", action="store_true", help="also delete all per-workspace data")
    pu.set_defaults(func=cmd_uninstall)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

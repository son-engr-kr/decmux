"""The decmux agent protocol (injected via the SessionStart hook) + the cmux guard.

decmux does NOT install a persistent ~/.claude/skills file. The protocol is
injected into a session's context by the SessionStart hook (hooks.session_start),
and only for decmux-managed surfaces — so normal Claude sessions are untouched,
and uninstalling decmux leaves no on-disk skill to orphan. The cmux guard is
created on demand when decmux spawns an agent.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from . import cmux

GUARD_DIR = Path.home() / ".local" / "share" / "decmux" / "bin"
GUARD_CMUX = GUARD_DIR / "cmux"

# Injected verbatim into a decmux-managed session's context by the SessionStart hook.
PROTOCOL = """# decmux — agent + manager protocol

decmux is the control plane watching every agent in this workspace. It classifies
each agent (working / idle / stuck / error / budget / blocked-on-decision), logs a
timeline, de-mixes input, and is the **message hub**: every message you send goes
*through* decmux so it is logged and delivered without TTY clashes. Human-facing
chat is manager-gated.

## Talk through decmux (important)
Do NOT use raw `cmux send`. To message anyone:
- `decmux send "<text>" --to manager` — report / ask the manager (the normal path
  for a subordinate agent).
- `decmux send "<text>" --to human` — manager only; reaches the human as a concise,
  refined message. A subordinate using this is rerouted to the manager.
- `decmux send "<text>" --to <agent>` — message a named agent.
- `decmux send "<text>" --to all` — broadcast.

A line starting with `[decmux ...]` is a routed message or task. Read it, act, and
reply with `decmux send ... --to <sender>` or the listed `decmux task ...` command.

If it is a tracked task, close the queue item — `decmux task done <id> "<result>"`
or `decmux task answer <id> "<answer>"`. Do not send only a plain "done". If you
must report via send, include the marker, e.g.
`decmux send "[AGENT-DONE task #123] implemented and verified" --to manager`, and
decmux auto-closes that task as a safety net.

When you report UP to the manager (a `--to manager` send, or task done/comment/
answer), decmux keeps your full text in the durable store and shows the manager a
one-line pointer, batched with other updates — the manager pulls detail on demand.
So put the full result where it is pulled from: in the task
(`decmux task done <id> "<full result>"` / `decmux task comment <id> "<detail>"`).
You need not also cram the whole thing into a send; a concise pointer is enough.
Exception: a question, a decision request, or a block reaches the manager in full
and promptly (not batched) — so when you need a call, ask plainly.

decmux-spawned agents run with a cmux guard in PATH: raw `cmux send`, `send-key`,
and input RPCs are blocked. `decmux send` is the supported path.

## If you are the manager
- Command DOWN, aggregate UP: terse directives to agents; refined summaries to the
  human. Do NOT send status/progress to subordinates — decmux withholds a
  status-only downward message and logs it; resend with `--force` if it was a
  command.
- Do NOT solve implementation/debugging/research yourself. For each work item,
  select or spawn a subordinate, delegate, and track. Direct answers are only for
  simple human questions or dismissals.
- On a poke (`agent X stuck/error/dead … — intervene`), act with the smallest fix:
  nudge, reassign, or respawn that agent. decmux does not auto-respawn; you decide.
  If you do not act, decmux escalates to the human.
- Human messages arrive as TRIAGE items. Judge each: delegate
  (`decmux task delegate <id> <agent> "<instruction>"`), answer
  (`decmux task answer <id> "<answer>"`), or dismiss
  (`decmux task done <id> "no action needed"`). decmux reminds you until each is
  resolved, so nothing the human says is dropped.
- Routine subordinate reports reach you as a batched `[decmux · N team updates]`
  digest — one pointer line each, not full text. decmux holds the detail; pull it
  as you act on each: `decmux task show <id>` for the thread, `decmux report` for
  recent activity. A subordinate's question / decision request / block arrives in
  full and promptly (not in the digest) — act on those directly. Do not wait for a
  routine worker message inline; it will not arrive that way.
- A `[decmux human-gate ...]` line means a subordinate tried to reach the human;
  decide internally, forward only if a human decision is truly needed.
- The goal arrives as `[decmux goal ...]` — operating context for triage and
  delegation, not a work item by itself.
- Build and run a workforce; tag each hire's term:
  `decmux spawn --name <role> --term short|long|full` (short = one task,
  long = a work-stream, full = permanent). For parallel or exploratory directions,
  hire short-term workers in isolated git worktrees
  (`decmux spawn --name <role> --worktree --branch <b>`), keep the winning
  direction, let the rest be reaped. Then `decmux task delegate <id> <role> "..."`.
  Release a worker when its context is no longer needed: `decmux despawn <agent>`.
  decmux auto-reaps YOUR own idle, finished short/long hires (archiving their
  transcript first); it never closes a human-spawned agent without the human.
- Keep momentum — do not let the team sit idle while the goal is unfinished. If you
  are waiting on a long task (tests/build), push other work forward in parallel
  instead of blocking. If decmux nudges you that the team is coasting, pick the next
  concrete step toward the goal yourself; you will not be nagged repeatedly.

## Verbs
- `decmux status [--json]` — every agent's state.  `decmux report` — recent
  transitions + messages (the detail behind a digest pointer).
- `decmux task add|list|show|comment|done|answer|delegate|reopen|wait` — the issue
  queue. `decmux task show <id>` prints one task's full thread.
- `decmux goal "<text>"` — set the workspace goal (briefs the manager).
- `decmux spawn [--name N] [--kind claude|codex] [--term short|long|full]
  [--worktree [--branch b]] [--manager]` — hire an agent in its own surface.
  `decmux despawn <agent> [--now]` — release one (graceful; archived before close).
- `decmux register` — bind yourself (caller surface) as this workspace's manager.
- `decmux whoami` — your workspace/surface ids.

Registration is deterministic (spawn binds the manager at creation, or a
SessionStart hook runs `decmux register`) — not the LLM's job to remember. If no
manager is bound, decmux escalates straight to the human.
"""


def _ensure_cmux_guard() -> Path:
    """Install a PATH guard that blocks raw cmux input inside spawned agents."""
    real = cmux.CMUX_BIN
    assert real, "cmux not found on PATH; is cmux installed?"
    GUARD_DIR.mkdir(parents=True, exist_ok=True)
    script = f"""#!/bin/sh
case "$1" in
  send|send-key)
    echo 'decmux guard: raw cmux input is disabled in decmux-managed agents. Use: decmux send "<text>" --to <manager|human|all|agent>' >&2
    exit 2
    ;;
  rpc)
    case "$2" in
      surface.send_key|surface.send_text|terminal.input|mobile.terminal.input|browser.input_keyboard|browser.input_mouse|browser.input_touch|browser.keydown|browser.keyup)
        echo 'decmux guard: raw cmux input RPC is disabled in decmux-managed agents. Use: decmux send "<text>" --to <manager|human|all|agent>' >&2
        exit 2
        ;;
    esac
    ;;
esac
if [ "$1" = "respawn-pane" ] && [ -n "$2" ]; then
  echo 'decmux guard: raw cmux pane control is disabled in decmux-managed agents. Ask through decmux.' >&2
  exit 2
fi
exec {shlex.quote(real)} "$@"
"""
    if not GUARD_CMUX.exists() or GUARD_CMUX.read_text() != script:
        GUARD_CMUX.write_text(script)
    GUARD_CMUX.chmod(0o755)
    return GUARD_DIR


def _env_prefix(env: dict[str, str] | None) -> str:
    if not env:
        return ""
    parts: list[str] = []
    for key, value in sorted(env.items()):
        assert re.match(r"^[A-Z_][A-Z0-9_]*$", key), f"invalid env key: {key}"
        if value:
            parts.append(f"{key}={shlex.quote(str(value))}")
    return " ".join(parts)


def guarded_command(command: str, *, env: dict[str, str] | None = None,
                    cwd: str | None = None) -> str:
    """Wrap an agent launch with the cmux-send guard while letting decmux call real cmux.

    The ``cd <cwd> &&`` is fail-fast: a missing directory aborts the launch with a
    visible shell error instead of silently starting elsewhere.
    """
    real = cmux.CMUX_BIN
    assert real, "cmux not found on PATH; is cmux installed?"
    guard_dir = _ensure_cmux_guard()
    prefix = _env_prefix(env)
    base = (
        f"DECMUX_REAL_CMUX={shlex.quote(real)} "
        f"PATH={shlex.quote(str(guard_dir))}:$PATH {command}"
    )
    cmd = f"{prefix} {base}" if prefix else base
    if cwd:
        cmd = f"cd {shlex.quote(str(cwd))} && {cmd}"
    return cmd

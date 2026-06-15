"""Self-install of decmux's Claude Code assets: the manager skill + the cmux guard.

`ensure()` writes the bundled SKILL.md into ~/.claude/skills/decmux/ (idempotent,
version-stamped) so a Claude agent learns the decmux protocol with no setup.
`guarded_command()` wraps an agent launch so raw `cmux send`/input is blocked and
must go through `decmux send` (the de-mixing guarantee).
"""

from __future__ import annotations

import re
import shlex
import shutil
from pathlib import Path

from . import __version__, cmux

GUARD_DIR = Path.home() / ".local" / "share" / "decmux" / "bin"
GUARD_CMUX = GUARD_DIR / "cmux"
SKILLS_DIR = Path.home() / ".claude" / "skills" / "decmux"
STAMP = SKILLS_DIR / ".version"

SKILL_MD = """---
name: decmux
description: Operate under decmux for this cmux workspace — route all messages through decmux (not raw cmux send), respond to its pokes, and drive it via the decmux CLI.
---
# decmux — agent + manager protocol

decmux is the control plane watching every agent in this workspace. It classifies
each agent (working / idle / stuck / error / budget / blocked-on-decision), logs a
timeline, de-mixes input, and is the **message hub**: every message you send goes
*through* decmux so it is logged and delivered without TTY clashes. Human-facing
chat is manager-gated.

## Talk through decmux (important)
Do NOT use raw `cmux send`. To message anyone:
- `decmux send "<text>" --to manager` — report / ask the manager (the normal path
  for a subordinate agent).
- `decmux send "<text>" --to you` — manager only; reaches the human as a concise,
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
- A `[decmux human-gate ...]` line means a subordinate tried to reach the human;
  decide internally, forward only if a human decision is truly needed.
- The goal arrives as `[decmux goal ...]` — operating context for triage and
  delegation, not a work item by itself.
- Spawn a subordinate when it helps: `decmux spawn --name <role> --kind claude`,
  then `decmux task delegate <id> <role> "<instruction>"`. Keep the team small.

## Verbs
- `decmux status [--json]` — every agent's state.  `decmux report` — recent timeline.
- `decmux task add|list|comment|done|answer|delegate|reopen|wait` — the issue queue.
- `decmux goal "<text>"` — set the workspace goal (briefs the manager).
- `decmux spawn [--name N] [--kind claude|codex] [--manager]` — new agent in its own surface.
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
    echo 'decmux guard: raw cmux input is disabled in decmux-managed agents. Use: decmux send "<text>" --to <manager|you|all|agent>' >&2
    exit 2
    ;;
  rpc)
    case "$2" in
      surface.send_key|surface.send_text|terminal.input|mobile.terminal.input|browser.input_keyboard|browser.input_mouse|browser.input_touch|browser.keydown|browser.keyup)
        echo 'decmux guard: raw cmux input RPC is disabled in decmux-managed agents. Use: decmux send "<text>" --to <manager|you|all|agent>' >&2
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


def ensure(force: bool = False) -> bool:
    """Write SKILL.md into ~/.claude/skills/decmux/ when missing/stale (idempotent)."""
    _ensure_cmux_guard()
    skill = SKILLS_DIR / "SKILL.md"
    stamp = STAMP.read_text() if STAMP.exists() else ""
    current = skill.read_text() if skill.exists() else ""
    if not force and stamp == __version__ and current == SKILL_MD:
        return False
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    skill.write_text(SKILL_MD)
    STAMP.write_text(__version__)
    return True


def remove() -> dict:
    """Delete the installed skill and the cmux guard. Leaves workspace data alone."""
    out = {"skill": False, "guard": False}
    if SKILLS_DIR.exists():
        shutil.rmtree(SKILLS_DIR)
        out["skill"] = True
    if GUARD_DIR.exists():
        shutil.rmtree(GUARD_DIR)
        out["guard"] = True
        parent = GUARD_DIR.parent          # ~/.local/share/decmux
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    return out

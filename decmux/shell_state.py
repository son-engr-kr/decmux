"""Track which agents are executing a shell command, from the cmux agent-hook
event stream (deterministic — no screen-scraping).

cmux forwards each agent's tool hooks as events: ``agent.hook.PreToolUse`` carries
``{tool_name, session_id, _ppid}`` when the agent invokes a tool, and
``agent.hook.Stop`` fires when a turn ends. A shell command is *in flight* from a
PreToolUse with a shell tool (``Bash``) until that session's next tool call, its
PostToolUse, the turn end (Stop), or a staleness timeout. ``_ppid`` is the agent
runtime's pid (== the surface's foreground pgid), so the watcher attributes a
running command to a surface by intersecting it with the surface's process tree.

This is the signal behind the "running a shell command" display state: a thinking
LLM generates tokens at ~0% CPU, while a shell command (build/test/etc.) is a
child process doing the work — the two are different activities and only the hook
stream tells them apart reliably.
"""

from __future__ import annotations

# The shell-execution tool. Claude and codex both name it "Bash"; "BashOutput"
# only polls an already-backgrounded stream, so it is not a fresh command.
SHELL_TOOLS = {"Bash"}

# Hook events that end whatever tool was in flight for a session.
_END_EVENTS = {"PostToolUse", "Stop", "SubagentStop"}


class ShellTracker:
    """Maps active shell-command executions to agent-runtime pids."""

    def __init__(self, ttl: float = 300.0) -> None:
        self.ttl = ttl  # backstop: drop a marker no event has closed (long builds)
        self._running: dict[str, tuple[int, float]] = {}  # session_id -> (ppid, started)

    def observe(self, *, name: str, payload: dict, now: float) -> None:
        """Feed one ``agent.hook.*`` event frame."""
        if not name.startswith("agent.hook."):
            return
        sid = payload.get("session_id")
        if not sid:
            return
        event = name.rsplit(".", 1)[-1]
        if event == "PreToolUse":
            tool = payload.get("tool_name") or ""
            ppid = payload.get("_ppid")
            if tool in SHELL_TOOLS and ppid:
                self._running[sid] = (int(ppid), now)
            else:
                # a different tool started -> the prior shell command has finished
                self._running.pop(sid, None)
        elif event in _END_EVENTS:
            self._running.pop(sid, None)

    def active_ppids(self, now: float) -> set[int]:
        """Runtime pids currently running a shell command (drops stale markers)."""
        live: dict[str, tuple[int, float]] = {}
        out: set[int] = set()
        for sid, (ppid, ts) in self._running.items():
            if now - ts < self.ttl:
                live[sid] = (ppid, ts)
                out.add(ppid)
        self._running = live
        return out

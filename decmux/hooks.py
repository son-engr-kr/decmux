"""Claude Code integration: a SessionStart hook that injects the decmux protocol.

No skill file is installed. The hook runs `decmux session-start`, which injects
the protocol as `additionalContext` — but only for decmux-managed surfaces (a
spawned agent has DECMUX_ROLE; a registered manager is found in its workspace
store). Normal Claude sessions get nothing. The hook command is self-guarding
(`command -v decmux ... || true`), so after `uv tool uninstall decmux` it is an
inert no-op and nothing is left to clean up.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import assets, cmux

CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
_SESSION_MARKER = "decmux session-start"
# self-guarding: a no-op when decmux is no longer installed
_SESSION_COMMAND = "command -v decmux >/dev/null 2>&1 && decmux session-start || true"


def _load() -> dict:
    return json.loads(CLAUDE_SETTINGS.read_text()) if CLAUDE_SETTINGS.exists() else {"hooks": {}}


def _save(data: dict) -> None:
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(json.dumps(data, indent=2) + "\n")


def _has_hook(entries: list[dict], marker: str) -> bool:
    return any(marker in h.get("command", "")
               for e in entries for h in e.get("hooks", []))


def _append(hooks: dict, event: str, command: str, marker: str, timeout: int) -> bool:
    entries = hooks.setdefault(event, [])
    if _has_hook(entries, marker):
        return False
    entries.append({"hooks": [{"command": command, "timeout": timeout, "type": "command"}]})
    return True


def install_session_hook() -> bool:
    data = _load()
    changed = _append(data.setdefault("hooks", {}), "SessionStart",
                      _SESSION_COMMAND, _SESSION_MARKER, 10)
    if changed:
        _save(data)
    return changed


def install_all_hooks() -> dict:
    return {"session_hook": install_session_hook()}


def _remove_matching(hooks: dict, event: str, needle: str) -> bool:
    entries = hooks.get(event)
    if not entries:
        return False
    kept = [e for e in entries
            if not any(needle in h.get("command", "") for h in e.get("hooks", []))]
    if len(kept) == len(entries):
        return False
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event, None)
    return True


def remove_session_hook() -> bool:
    """Remove the decmux SessionStart hook from settings.json (the inverse of setup)."""
    if not CLAUDE_SETTINGS.exists():
        return False
    data = _load()
    changed = _remove_matching(data.setdefault("hooks", {}), "SessionStart", _SESSION_MARKER)
    if changed:
        _save(data)
    return changed


def claude_status() -> dict:
    data = _load()
    return {
        "session_start_hook": _has_hook(data.get("hooks", {}).get("SessionStart", []),
                                        _SESSION_MARKER),
        "settings": str(CLAUDE_SETTINGS),
    }


def _is_decmux_session() -> bool:
    """True only for surfaces decmux manages — so we don't inject into normal sessions."""
    if os.environ.get("DECMUX_ROLE"):           # decmux-spawned agent/manager
        return True
    try:                                         # a register-bound manager
        c = cmux.run_json("identify", "--id-format", "both", "--json")["caller"]
    except (subprocess.CalledProcessError, OSError, KeyError):
        return False
    ws, sid = c.get("workspace_id"), c.get("surface_id")
    if not ws or not sid:
        return False
    from .store import Store, _root
    if not (_root() / ws / "store.db").exists():   # don't create a store just to check
        return False
    return Store(ws).is_manager(sid)


def session_start() -> int:
    """SessionStart hook entry: inject the protocol for decmux sessions, else nothing."""
    if _is_decmux_session():
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": assets.PROTOCOL}}))
    else:
        print(json.dumps({}))
    return 0

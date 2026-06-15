"""Claude Code integration: a SessionStart hook that reloads the decmux skill.

The old prompt->task hooks proved too noisy (every agent prompt became a task), so
they are not installed and any leftovers are removed. The SessionStart hook keeps
the decmux skill fresh each session.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import assets

CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
_SESSION_MARKER = "decmux session-start"


def _session_command() -> str:
    return "decmux session-start"


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


def install_session_hook() -> bool:
    assets.ensure()
    data = _load()
    changed = _append(data.setdefault("hooks", {}), "SessionStart",
                      _session_command(), _SESSION_MARKER, 10)
    if changed:
        _save(data)
    return changed


def uninstall_prompt_hooks() -> bool:
    """Remove any legacy decmux UserPromptSubmit prompt->task hooks."""
    if not CLAUDE_SETTINGS.exists():
        return False
    data = _load()
    changed = _remove_matching(data.setdefault("hooks", {}), "UserPromptSubmit", "decmux")
    if changed:
        _save(data)
    return changed


def install_all_hooks() -> dict:
    removed = uninstall_prompt_hooks()
    return {"session_hook": install_session_hook(), "removed_legacy_prompt": removed}


def remove_hooks() -> dict:
    """Remove decmux's Claude Code hooks (SessionStart + any legacy prompt hooks)."""
    if not CLAUDE_SETTINGS.exists():
        return {"session_removed": False, "prompt_removed": False}
    data = _load()
    hooks = data.setdefault("hooks", {})
    session_removed = _remove_matching(hooks, "SessionStart", _SESSION_MARKER)
    prompt_removed = _remove_matching(hooks, "UserPromptSubmit", "decmux")
    if session_removed or prompt_removed:
        _save(data)
    return {"session_removed": session_removed, "prompt_removed": prompt_removed}


def claude_status() -> dict:
    skill = assets.SKILLS_DIR / "SKILL.md"
    data = _load()
    return {
        "session_start_hook": _has_hook(data.get("hooks", {}).get("SessionStart", []),
                                        _SESSION_MARKER),
        "skill": skill.exists(),
        "settings": str(CLAUDE_SETTINGS),
    }


def session_start() -> int:
    """SessionStart hook entry: refresh the skill and tell Claude to reload it."""
    assets.ensure()
    print(json.dumps({"reloadSkills": True}))
    return 0

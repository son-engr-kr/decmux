"""decmux configuration (TOML) with sane defaults.

Loaded from ``~/.config/decmux/config.toml`` (override via ``DECMUX_CONFIG``).
Per-workspace settings (keyed by cwd or a slug) fall back to ``[defaults]``;
per-agent capability profiles live under ``[agents.<name>]``.

Fail-fast: unknown keys raise (a config typo should crash, not be ignored).

Foreground model: keys for the dropped overnight-autonomy layer (night mode,
autoscale, idle-reap, budget caps, discord) are intentionally gone.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(
    os.environ.get("DECMUX_CONFIG", Path.home() / ".config" / "decmux" / "config.toml")
)


@dataclass
class WorkspaceConfig:
    # watchdog thresholds
    idle_after: float = 30.0          # seconds quiet before "idle"
    stuck_after: float = 300.0        # seconds quiet before "stuck"
    busy_cpu: float = 1.0             # %CPU above this = "working"
    hysteresis_polls: int = 2         # polls a new state must hold before committing
    # stuck-handling (poke the manager, then escalate to the human)
    stuck_poke_after: float = 60.0    # seconds in stuck/error/dead before poking the manager
    escalation_timeout: float = 300.0  # manager unresponsive this long -> human
    manager: str | None = None        # manager surface selector
    # auto-answer of safe/reversible Feed permission requests while decmux is open
    auto_answer: bool = False         # off by default; the human/manager answers
    # workforce lifecycle: self-spawned agents are auto-reaped when idle + done;
    # human-spawned ones are never auto-closed (the human confirms via /despawn).
    reap_short_grace: float = 120.0   # idle seconds before a short-term self agent is reaped
    reap_long_grace: float = 600.0    # idle seconds before a long-term self agent is reaped
    # proactive momentum: when the team coasts (idle, goal unfinished), nudge the
    # manager ONCE to advance — never a repeated nag.
    momentum: bool = True             # master switch for the coast nudge
    momentum_cooldown: float = 300.0  # min seconds between coast nudges (anti-nag floor)


@dataclass
class Config:
    defaults: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    workspaces: dict[str, WorkspaceConfig] = field(default_factory=dict)
    agents: dict[str, dict] = field(default_factory=dict)

    def for_workspace(self, *keys: str | None) -> WorkspaceConfig:
        """Return config for the first matching key (e.g. cwd then name)."""
        for key in keys:
            if key and key in self.workspaces:
                return self.workspaces[key]
        return self.defaults


def load(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text())
    base = data.get("defaults", {})
    defaults = WorkspaceConfig(**base)
    workspaces = {
        key: WorkspaceConfig(**{**base, **overrides})
        for key, overrides in data.get("workspaces", {}).items()
    }
    return Config(defaults=defaults, workspaces=workspaces, agents=data.get("agents", {}))

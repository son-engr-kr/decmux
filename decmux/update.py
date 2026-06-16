"""Self-update check: compare the installed version with the latest on GitHub and,
if newer, offer to reinstall. The check is best-effort — no network, a slow mirror,
or a dev checkout just means "no update offered", never an error.
"""

from __future__ import annotations

import re
import subprocess
import urllib.request
from pathlib import Path

REPO = "https://github.com/son-engr-kr/decmux"
# raw __init__.py on the default branch carries the canonical __version__
_RAW_INIT = "https://raw.githubusercontent.com/son-engr-kr/decmux/main/decmux/__init__.py"


def current_version() -> str:
    from . import __version__
    return __version__


def _vtuple(s: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", s or "")[:3])


def _parse_version(text: str) -> str | None:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text or "")
    return m.group(1) if m else None


def is_editable() -> bool:
    """True when running from a source checkout (uv `--editable` / a clone), where the
    right update is `git pull`, not a reinstall — so we never clobber a dev tree."""
    p = str(Path(__file__).resolve())
    return "uv/tools" not in p and "site-packages" not in p


def latest_version(timeout: float = 3.0) -> str | None:
    try:
        with urllib.request.urlopen(_RAW_INIT, timeout=timeout) as r:  # noqa: S310 (fixed https URL)
            return _parse_version(r.read(4096).decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None


def update_available(timeout: float = 3.0) -> tuple[str, str] | None:
    """(current, latest) when GitHub has a newer version, else None."""
    cur = current_version()
    latest = latest_version(timeout=timeout)
    if latest and _vtuple(latest) > _vtuple(cur):
        return cur, latest
    return None


def run_install(version: str) -> bool:
    """Reinstall the pinned tag from GitHub. Returns True on success."""
    proc = subprocess.run(
        ["uv", "tool", "install", "--force", f"git+{REPO}@v{version}"],
        capture_output=True, text=True)
    return proc.returncode == 0

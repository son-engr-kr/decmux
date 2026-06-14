"""Thin wrapper around the `cmux` CLI — decmux's link to cmux.

decmux talks to cmux by shelling out to the `cmux` binary rather than
reimplementing the Unix-socket protocol: cmux already handles auth and framing,
and `cmux events` gives a clean newline-delimited JSON stream. decmux runs inside
a cmux-hosted surface, so the socket's default cmuxOnly control mode admits it
without passing a socket path/password.

Fail-fast: every call uses check=True, so a non-zero exit raises with the full
stderr instead of being silently swallowed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator


def _find_cmux() -> str | None:
    override = os.environ.get("DECMUX_REAL_CMUX")
    if override:
        return override
    found = shutil.which("cmux")
    if found:
        return found
    # A surface started with a minimal PATH may lack cmux; fall back to the app.
    candidate = "/Applications/cmux.app/Contents/Resources/bin/cmux"
    return candidate if os.path.exists(candidate) else None


CMUX_BIN = _find_cmux()

# Suppress cmux alias/deprecation notices so they never contaminate output.
_ENV = {**os.environ, "CMUX_QUIET": "1"}


def _require_cmux() -> str:
    assert CMUX_BIN, "cmux not found on PATH; is cmux installed?"
    return CMUX_BIN


def run(*args: str) -> str:
    """Run `cmux <args>` and return stdout. Raises on non-zero exit."""
    cmux = _require_cmux()
    result = subprocess.run(
        [cmux, *args],
        check=True,
        capture_output=True,
        text=True,
        env=_ENV,
    )
    return result.stdout


def run_json(*args: str):
    """Run a cmux command that emits JSON and return the parsed value."""
    return json.loads(run(*args))


def feed_reply_permission(request_id: str, mode: str) -> str:
    """Answer a Feed permission request. mode in once|always|all|bypass|deny."""
    return run("rpc", "feed.permission.reply",
               json.dumps({"request_id": request_id, "mode": mode}))


def feed_reply_question(request_id: str, selections: list[str]) -> str:
    """Answer a Feed AskUserQuestion with the chosen option label(s)."""
    return run("rpc", "feed.question.reply",
               json.dumps({"request_id": request_id, "selections": selections}))


def read_screen(surface_id: str, *, workspace: str | None = None, lines: int = 40) -> str:
    """Return the recent on-screen text of a surface (for pattern detection).

    Pass the surface's workspace so the ref/uuid resolves to the right surface.
    """
    args = ["read-screen"]
    if workspace:
        args += ["--workspace", workspace]
    args += ["--surface", surface_id, "--lines", str(lines)]
    return run(*args)


def _events_args(categories, names, cursor_file, reconnect, no_heartbeat) -> list[str]:
    args = ["events"]
    for category in categories or []:
        args += ["--category", category]
    for name in names or []:
        args += ["--name", name]
    if cursor_file:
        args += ["--cursor-file", cursor_file]
    if reconnect:
        args.append("--reconnect")
    if no_heartbeat:
        args.append("--no-heartbeat")
    return args


def events_popen(
    *,
    categories: list[str] | None = None,
    names: list[str] | None = None,
    cursor_file: str | None = None,
    reconnect: bool = True,
    no_heartbeat: bool = False,
) -> subprocess.Popen:
    """Spawn `cmux events` and return the process (caller reads stdout/terminates)."""
    cmux = _require_cmux()
    args = _events_args(categories, names, cursor_file, reconnect, no_heartbeat)
    return subprocess.Popen([cmux, *args], stdout=subprocess.PIPE, text=True, env=_ENV)


def events(
    *,
    categories: list[str] | None = None,
    names: list[str] | None = None,
    cursor_file: str | None = None,
    reconnect: bool = True,
    no_heartbeat: bool = False,
) -> Iterator[dict]:
    """Stream `cmux events` as parsed JSON frames, one per line."""
    proc = events_popen(
        categories=categories, names=names, cursor_file=cursor_file,
        reconnect=reconnect, no_heartbeat=no_heartbeat,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if line:
                yield json.loads(line)
    finally:
        proc.terminate()

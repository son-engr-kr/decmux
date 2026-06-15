"""The watchdog: deterministic state classification for cmux agent surfaces.

Principle (see DESIGN.md): state is a pure function of observable signals + time.
`classify()` never guesses and never forgets — same inputs, same output. The
`Watcher` adds the only stateful ingredient, a per-surface activity clock keyed
by stable surface UUID, so idle vs stuck can be told apart.

Signals come from one `cmux top --all --id-format both --format tsv` call (CPU%,
memory, live process count, title, and stable UUIDs) plus `cmux workspace list`
for each workspace's cwd. cmux also emits a per-workspace `claude_code` tag whose
value ("Running" / "Needs input") is agent-truth we receive rather than infer.

This store is one workspace, so the watcher filters `cmux top` (which spans every
workspace) down to agents in its own workspace.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass

from . import cmux, errors
from . import config as config_mod

AGENT_MARK = "✳"

# read-screen negative results are retried quickly because terminal content can
# change between cmux frames. Positive error/limit detections are cached longer.
SCREEN_THROTTLE = 10.0
SCREEN_STATE_TTL = 120.0

# A terminal surface is an agent if its process tree runs an agent runtime
# (titles are unreliable — users rename agents "opus", "codex", "Fable5", ...).
_AGENT_PROCS = {"node", "codex", "fable", "bun", "deno", "gemini", "opencode", "claude"}
_VER = re.compile(r"^\d+\.\d+\.\d+")  # claude/agent CLI shows its version as the proc name

# Shells an agent runtime spawns to run a Bash tool — foreground OR a persistent
# backgrounded one (Claude Code's "N shell" indicator). The discriminator: such a
# shell is a child of the agent RUNTIME pid, whereas the surface's own login shell
# is a child of the SURFACE, so the login shell never trips this.
_SHELL_NAMES = {"sh", "bash", "zsh", "fish", "dash", "ksh"}


def _is_runtime(name: str) -> bool:
    """Does this process name look like an agent runtime (version / known CLI)?"""
    n = name.strip().lower()
    return bool(_VER.match(name.strip())) or n in _AGENT_PROCS or "python" in n


def _runtime_shell(proc_rows) -> bool:
    """True when a shell process is a child of the agent runtime — i.e. a Bash tool
    (foreground or a live background shell) is running. ``proc_rows`` are
    (pid, parent, name): a direct child of the surface has a non-numeric parent
    (the surface id), a deeper process has its parent pid, so the runtime's own
    children are exactly the shells/commands it spawned."""
    runtime_pids = {pid for pid, parent, name in proc_rows
                    if not parent.isdigit() and _is_runtime(name)}
    if not runtime_pids:
        return False
    return any(parent in runtime_pids
               and name.strip().lower().lstrip("-") in _SHELL_NAMES
               for pid, parent, name in proc_rows)


# Model + effort live in the agent's bottom status footer, not the transcript.
# Match the model family + its version token only (don't swallow the rest of the
# line), and read effort from a [bracket] (claude) or a bare word (codex footer).
_MODEL = re.compile(
    r"\b(opus|sonnet|haiku|fable|gpt|gemini|claude)[\w.\-]*"   # family (+ gpt-5.5)
    r"(?:\s+\d+(?:\.\d+)?)?"                                    # optional " 4.7"
    r"(?:\s*\(1M[^)]*\))?",                                     # optional " (1M context)"
    re.IGNORECASE)
_EFFORT = re.compile(r"\[(x?high|x?low|medium|max|minimal|none)\]", re.IGNORECASE)
_EFFORT_BARE = re.compile(r"\b(x?high|x?low|medium|minimal)\b", re.IGNORECASE)


def _last(rx: re.Pattern, s: str):
    last = None
    for last in rx.finditer(s):
        pass
    return last


def _parse_model_effort(text: str) -> tuple[str, str]:
    tail = "\n".join((text or "").splitlines()[-8:])  # footer only, not the chat above
    # take the bottom-most match: the persistent status footer, not a stray model
    # name mentioned in the transcript that scrolled into the tail.
    m = _last(_MODEL, tail)
    model = re.sub(r"\s+", " ", m.group(0)).strip()[:40] if m else ""
    e = _last(_EFFORT, tail) or _last(_EFFORT_BARE, tail)
    return model, (e.group(1) if e else "")


def _is_agent(title: str, procs: list[str]) -> bool:
    if title.strip().lower() in ("decmux", "decmux office"):
        return False  # decmux's own surfaces
    if title.startswith(AGENT_MARK):
        return True
    for p in procs:
        pl = p.lower()
        if _VER.match(p) or pl in _AGENT_PROCS or "python" in pl:
            return True
    return False


@dataclass
class Surface:
    ref: str
    uuid: str
    pane: str
    workspace: str          # workspace ref (ephemeral)
    workspace_uuid: str     # workspace UUID (stable)
    title: str
    cpu: float
    mem: int
    procs: int
    agent: bool = False
    proc_names: tuple = ()
    pids: tuple = ()        # pids in this surface's process tree (shell attribution)
    runtime_shell: bool = False   # a shell is running under the agent runtime

    @property
    def is_agent(self) -> bool:
        return self.agent

    @property
    def key(self) -> str:
        """Stable identity for state tracking."""
        return self.uuid or self.ref


@dataclass
class Row:
    surface: Surface
    state: str
    ws_name: str
    ws_agent_tag: str | None
    quiet_for: float | None
    workspace_cwd: str = ""
    model: str = ""
    effort: str = ""
    busy_kind: str = ""     # when state=working: 'shell' (running a command) | 'llm' | ''


def _split_id(field: str) -> tuple[str, str]:
    """`--id-format both` renders ids as 'ref UUID'."""
    ref, _, uuid = field.partition(" ")
    return ref, uuid


def parse_top(tsv: str):
    """Parse `cmux top --id-format both --processes --format tsv`.

    Returns (surfaces, ws_name{ws_ref: name}, ws_agent{ws_ref: claude status}).
    Process rows are attributed to the surface they follow (for agent detection).
    """
    pane_ws: dict[str, tuple[str, str]] = {}   # pane ref -> (ws ref, ws uuid)
    ws_name: dict[str, str] = {}
    ws_agent: dict[str, str] = {}
    procs_by_ref: dict[str, list[str]] = {}
    pids_by_ref: dict[str, list[int]] = {}
    tree_by_ref: dict[str, list[tuple]] = {}   # surface -> [(pid, parent, name)]
    raw: list[tuple] = []
    current: str | None = None

    for line in tsv.splitlines():
        p = line.split("\t")
        if len(p) < 5:
            continue
        kind = p[3]
        if kind == "process":
            if current is not None:
                nm = p[6] if len(p) > 6 else ""
                procs_by_ref.setdefault(current, []).append(nm)
                if p[4].isdigit():   # pid -> surface, for shell-command attribution
                    pids_by_ref.setdefault(current, []).append(int(p[4]))
                tree_by_ref.setdefault(current, []).append(
                    (p[4], p[5] if len(p) > 5 else "", nm))
            continue
        cpu, mem, procs = float(p[0]), int(p[1]), int(p[2])
        ref, uuid = _split_id(p[4])
        parent_ref, parent_uuid = _split_id(p[5]) if len(p) > 5 else ("", "")
        label = p[6] if len(p) > 6 else ""

        if kind == "pane":
            pane_ws[ref] = (parent_ref, parent_uuid)
            current = None
        elif kind == "workspace":
            ws_name[ref] = label
            current = None
        elif kind == "tag" and ":tag:claude_code" in ref:
            ws_agent[parent_ref] = label
            current = None
        elif kind == "surface":
            current = ref
            raw.append((cpu, mem, procs, ref, uuid, parent_ref, label))
        else:
            current = None

    surfaces = []
    for cpu, mem, procs, ref, uuid, pane, title in raw:
        ws_ref, ws_uuid = pane_ws.get(pane, ("", ""))
        pns = procs_by_ref.get(ref, [])
        surfaces.append(
            Surface(ref, uuid, pane, ws_ref, ws_uuid, title, cpu, mem, procs,
                    _is_agent(title, pns), tuple(pns), tuple(pids_by_ref.get(ref, [])),
                    _runtime_shell(tree_by_ref.get(ref, [])))
        )
    return surfaces, ws_name, ws_agent


def _workspace_cwds() -> dict[str, str]:
    data = cmux.run_json("workspace", "list", "--json")
    return {w["ref"]: w.get("current_directory", "") for w in data.get("workspaces", [])}


# An agent's "busy" spinner, matched structurally rather than by a fixed verb
# list (Claude rotates dozens of gerunds per release, so an enumerated list rots).
# STRONG signals — present only while a turn is actively running, never in static
# UI: the "esc to interrupt/cancel" hint and the live token meter "↑/↓ <n>k tokens".
_BUSY_STRONG = re.compile(
    r"esc to (?:interrupt|cancel)|[↑↓]\s*[\d.]+\s*k?\s*tokens", re.IGNORECASE)
# The spinner gerund ("Deciphering…"). Weaker, so matched only on NON-boxed lines:
# Claude's welcome / "What's new" / tips panels live inside `│` box borders and
# contain gerunds like "credential caching…" that must NOT read as working.
_BUSY_GERUND = re.compile(r"\b\w+ing…")


def _screen_status(text: str) -> str | None:
    """Authoritative working/idle from the agent's screen — CPU is a poor proxy
    (a thinking agent uses ~0% CPU; a background process trips a high CPU).

    A *completed* turn reads "Cogitated for 1m 51s" (past tense, no live meter) and
    the idle "/clear to save 512.4k tokens" footer has no ↑/↓ arrow, so neither
    reads as working."""
    if not text:
        return None
    lines = text.splitlines()[-16:]
    if _BUSY_STRONG.search("\n".join(lines)):
        return "working"
    if any("│" not in ln and _BUSY_GERUND.search(ln) for ln in lines):
        return "working"
    return "idle"


def classify(cpu: float, procs: int, last_active: float | None, now: float,
             stuck_after: float, busy_cpu: float) -> str:
    """Pure: map one surface's signals to a state. Priority-ordered."""
    if procs == 0:
        return "dead"
    if cpu > busy_cpu:
        return "working"
    if last_active is None:
        return "idle"
    if now - last_active >= stuck_after:
        return "stuck"
    return "idle"


class Watcher:
    """Per-surface activity clock + hysteresis (keyed by stable surface UUID),
    scoped to one workspace."""

    def __init__(self, workspace_uuid: str, cfg: config_mod.Config | None = None,
                 detect_errors: bool = True) -> None:
        self.workspace_uuid = workspace_uuid
        self.config = cfg or config_mod.load()
        self.detect_errors = detect_errors
        self.last_active: dict[str, float] = {}
        self.committed: dict[str, str] = {}
        self.candidate: dict[str, tuple[str, int]] = {}
        self._activity: float = 0.0        # last event-stream activity for this workspace
        self._screen_checked: dict[str, float] = {}
        self._screen_state: dict[str, tuple[str, float]] = {}
        self.model: dict[str, str] = {}
        self.effort: dict[str, str] = {}

    def note_activity(self, now: float | None = None) -> None:
        """Record event-stream activity for this workspace (push signal from the session)."""
        self._activity = now if now is not None else time.time()

    def _detect_error(self, surface: Surface, now: float) -> str | None:
        if now - self._screen_checked.get(surface.key, 0.0) < SCREEN_THROTTLE:
            cached = self._screen_state.get(surface.key)
            if cached and now - cached[1] < SCREEN_STATE_TTL:
                return cached[0]
            return None
        self._screen_checked[surface.key] = now
        try:
            text = cmux.read_screen(surface.ref, workspace=surface.workspace, lines=40)
        except subprocess.CalledProcessError:
            return None  # surface not readable this tick (detached / not a terminal)
        self.model[surface.key], self.effort[surface.key] = _parse_model_effort(text)
        # errors/budget win; otherwise the screen tells working vs idle (CPU lies:
        # a thinking agent is ~0% CPU, an idle one with a side process is not).
        detected = errors.detect(text) or _screen_status(text)
        if detected:
            self._screen_state[surface.key] = (detected, now)
        else:
            self._screen_state.pop(surface.key, None)
        return detected

    def _commit(self, key: str, target: str, need: int) -> str:
        """Hysteresis: a new state must hold `need` consecutive polls to commit."""
        cur = self.committed.get(key)
        if cur is None:
            self.committed[key] = target
            return target
        if target == cur:
            self.candidate.pop(key, None)
            return cur
        cand, count = self.candidate.get(key, (target, 0))
        count = count + 1 if cand == target else 1
        if count >= need:
            self.committed[key] = target
            self.candidate.pop(key, None)
            return target
        self.candidate[key] = (target, count)
        return cur

    def poll(self, now: float | None = None,
             shell_ppids: set[int] | None = None) -> list[Row]:
        now = now if now is not None else time.time()
        shell_ppids = shell_ppids or set()
        surfaces, ws_name, ws_agent = parse_top(
            cmux.run("top", "--all", "--id-format", "both", "--processes", "--format", "tsv")
        )
        ws_cwd = _workspace_cwds()
        # only this workspace's agents (cmux top spans every workspace)
        agents = [s for s in surfaces
                  if s.is_agent and s.workspace_uuid == self.workspace_uuid]
        single_agent = len(agents) == 1

        rows: list[Row] = []
        for s in agents:
            self.last_active.setdefault(s.key, now)
            cwd = ws_cwd.get(s.workspace, "")
            name = ws_name.get(s.workspace, s.workspace)
            cfg = self.config.for_workspace(cwd, name)
            last = self.last_active[s.key]

            detected = self._detect_error(s, now) if self.detect_errors and s.procs else None
            if detected == "working":
                self.last_active[s.key] = now
                last = now
                target = "working"
            elif detected in ("error", "budget"):
                target = detected
            elif detected == "idle":
                # screen says not generating -> idle/stuck by time, never CPU-"working"
                target = classify(0.0, s.procs, last, now, cfg.stuck_after, cfg.busy_cpu)
            elif s.cpu > cfg.busy_cpu:
                # screen unreadable this tick: weak CPU fallback
                self.last_active[s.key] = now
                last = now
                target = "working"
            else:
                target = classify(s.cpu, s.procs, last, now, cfg.stuck_after, cfg.busy_cpu)

            # A shell command running for this agent means it is working even when
            # the screen shows no LLM spinner (the LLM is blocked on the tool, or
            # the shell is a persistent background one). Two complementary signals:
            #   - the hook stream: instant, foreground PreToolUse(Bash) window;
            #   - the process tree: a shell child of the runtime, which also covers
            #     a long-running BACKGROUND shell (Claude's "N shell").
            # Never let an agent with a live shell read as idle.
            shell = s.runtime_shell or (bool(shell_ppids) and bool(set(s.pids) & shell_ppids))
            if shell and target in ("idle", "stuck"):
                self.last_active[s.key] = now
                last = now
                target = "working"

            # Event-stream activity proves this workspace is alive: never "stuck".
            if target == "stuck" and now - self._activity < cfg.stuck_after:
                target = "idle"

            tag = ws_agent.get(s.workspace)
            # cmux's per-workspace claude_code tag is agent-truth; attribute a
            # "Needs input" only when the workspace has a single agent.
            if target != "budget" and tag and "need" in tag.lower() and single_agent:
                target = "blocked-on-decision"

            need = 1 if target in ("budget", "error") else cfg.hysteresis_polls
            state = self._commit(s.key, target, need)
            # working·shell whenever a shell (foreground or background) is alive for
            # this agent; otherwise working·llm. Only meaningful while state==working.
            busy_kind = ""
            if state == "working":
                busy_kind = "shell" if shell else "llm"
            rows.append(Row(s, state, name, tag, now - last, cwd,
                            self.model.get(s.key, ""), self.effort.get(s.key, ""), busy_kind))
        return rows

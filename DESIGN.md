# decmux — design (v2 rewrite)

A per-workspace, foreground control plane for a small team of `cmux`-hosted AI
coding agents. One manager + a few workers per project; `decmux` is the human's
single de-mixed interface to them.

This is a from-scratch rewrite of the original `decmux` (archived). It keeps the
hard-won supervision logic (captured below) and drops everything that assumed a
background daemon, a web UI, or cross-workspace aggregation.

## Model (decided)

1. **One per workspace, fully isolated.** Each workspace (= one cmux workspace =
   one project) has its own SQLite store and its own session. No shared daemon,
   no cross-workspace state.
2. **Foreground only — nothing runs in the background.** When you close
   `decmux`, supervision stops. There is no launchd service, no detached daemon.
3. **State is durable.** Tasks, chat, agent assignments, and the outbox live in
   the per-workspace SQLite store, so closing and reopening `decmux` remembers
   everything.
4. **`decmux` enters the current workspace's session.** Run with no args inside
   a cmux surface; it detects the caller's workspace (`cmux identify`), opens (or
   creates) that workspace's store, reconciles against live surfaces, and drops
   you into the interactive control program.

### The single process is both faces

While open, the `decmux` process simultaneously runs:

- the **interactive control program** — chat to the manager, issue commands, see
  agent status and the inter-agent message flow; and
- the **supervision loop** — classify each agent, queue/de-mix message delivery,
  route Feed decisions, poke on stalls.

cmux remains the *window*: you switch to any agent surface to watch the real
Claude Code session at full fidelity. `decmux` never abstracts that away — it
only provides what cmux does not (a de-mixed input channel, a queue, a task
store, classification, and reconcile).

### Reconcile on every launch

Every launch is a "restart." `decmux` loads durable state from the store and
re-attaches to live agents by **stable surface UUID** (`cmux top`), pruning
surfaces that are gone. A still-running agent is re-attached, never re-spawned.
This is more central here than in the old design, because launching is the
normal case, not a rare recovery.

## Non-goals (explicit)

- **No unattended / overnight supervision.** While `decmux` is closed, agents run
  unsupervised: no queued delivery, no classification, no pokes. The old night
  mode, morning report, auto-respawn-with-backoff, usage-limit auto-resume,
  budget-cap pause, autoscale, and idle-reap are **dropped**. (Re-evaluate only
  if a "run while I sleep" need returns — it would require a background process.)
- **No web UI** (the old `office`) and **no Discord** frontend.
- **No global daemon / cross-workspace single pane.** A `decmux ls` that scans
  the state dir for known workspaces is the only cross-workspace affordance.

## Long-running managing (goal + stuck-handling)

The everyday use case: open `decmux` in a workspace, set a goal, and let the
manager drive a small team toward it for hours — a *long-running session*, not a
background daemon. Two pieces make this reliable.

### Goal

`decmux goal "<text>"` (and a `/goal <text>` chat line) sets the workspace's
operating goal and delivers it to the bound manager as **operating context**, not
a tracked task. The goal persists in the store and is re-delivered to a manager
that (re)binds, so a manager started mid-session is briefed. It frames the
manager's triage and delegation; code never auto-decomposes it into tasks.

### Stuck-handling (the control plane pushes the manager)

The observed failure: an agent goes `stuck` (or `error`/`dead`) and the
manager-LLM, busy or forgetful, leaves it idle. This is exactly the "an LLM is an
unreliable supervisor" problem, so code — not the manager — owns the timer:

- When the watchdog holds an agent in `stuck`/`error`/`dead` for
  `stuck_poke_after` (default 60s), decmux **pokes the bound manager** with a
  terse directive naming the agent, its state, how long, and its title:
  *"agent X stuck 4m on '<title>' — intervene (nudge / reassign / respawn)."* The
  manager decides and acts; decmux does **not** auto-respawn (that dropped
  overnight autonomy stays dropped).
- The poke is de-mixed like any message: queued to the manager's outbox and
  delivered one-per-idle-turn, so it never corrupts the manager's live turn.
- It fires **once per stuck episode**, re-armed only after the agent recovers, so
  a persistently stuck agent is not spammed.
- If no manager is bound, or the manager does not resolve it within
  `escalation_timeout`, decmux **escalates to the human** (a `cmux notify` plus a
  line in the interactive program) — the only path that reaches the human
  directly.

Net: while `decmux` is open, no stuck agent rots unseen — code guarantees the
manager is told, and the human is told if the manager does not act.

## Stack

Python (managed with `uv`). The supervision loop is single-threaded async. The
interactive program is a terminal app — framework TBD (see Open questions).

## What carries over vs. what is dropped

Carried over from the old code, largely intact (these encode the value):

| Module (old → new) | Purpose |
| --- | --- |
| `cmux.py` → `cmux.py` | cmux CLI/socket client (subprocess; `CMUX_QUIET=1`; events stream) |
| `errors.py` → `errors.py` | self-framing error / usage-limit detection |
| `shell_state.py` → `shell_state.py` | hook-stream shell tracking |
| `watch.py` → `watch.py` | 6-state classification, hysteresis, screen/proc-tree signals |
| `store.py` → `store.py` | SQLite store — **simplified to single-workspace** (drop `ws` columns) |
| `bus.py` → `bus.py` | message routing, de-mix delivery, outbox, human-gate, task lifecycle |
| `policy.py` → `policy.py` | auto-vs-escalate decision policy |
| `assets.py` → `assets.py` | decmux skill + cmux-send guard |
| `codex_hook.py` → `hooks.py` | agent prompt/session hooks (prompt→task off by default) |
| `config.py` → `config.py` | per-workspace config (simplified) |

New:

- `session.py` — the per-workspace session: owns the store, the watcher, the
  bus, and the run loop. This is what `decmux` opens. (Replaces `daemon.py`.)
- `app.py` — the interactive control program (chat + commands + status + feed).
- `cli.py` — entry point. `decmux` (no args) → detect workspace → open session.
  Plus non-interactive verbs for agents/scripts to call (`decmux send`,
  `decmux task ...`, `decmux whoami`, `decmux register`, `decmux ls`).

Dropped entirely: `web.py`, `office.html`, `daemon.py` (folded into `session.py`),
`service.py` (no launchd), `discord_bot.py`, `updates.py` (optional later), and the
overnight-autonomy parts of `auto.py`.

## Data model (per-workspace SQLite)

One store per workspace at `~/.local/state/decmux/<workspace_uuid>/store.db`;
file attachments under `.../files/`. Because a store *is* a workspace, the old
`ws` / `workspace_uuid` scoping columns are dropped (implicit). Timestamps are
REAL epoch seconds. Identity is the stable `surface_uuid` (never `surface_ref`,
which churns across respawns). Migrations are additive only.

Tables:

- **`agent_state`** (PK `surface_uuid`): `surface_ref`, `title`, `state`,
  `model`/`effort` (sticky via COALESCE), `last_active`, `procs` (JSON),
  `busy_kind` (`''`/`shell`/`llm`, meaningful only while `working`), `note`,
  `updated_at`. The reconcile anchor: `last_states()` on launch, `prune_absent()`
  each tick.
- **`binding`** (singleton): the one manager surface for this workspace
  (`surface_uuid`, `surface_ref`, `cwd`, `updated_at`).
- **`tasks`** (PK `id`): `kind` (`question`/`command`), `body`, `to_whom`,
  `assignee`, `status` (`triage`/`open`/`in_progress`/`done`/`answered`),
  `progress`, `result`, `source`, `author`, `source_id` (dedup key),
  `delivered` (count), `delivered_at`, `last_reminded_at`, `reminder_count`,
  `escalated_at`, `closed_at`, `updated_at`. Idempotent create on `source_id`.
- **`task_comments`** (PK `id`): append-only timeline — `created`, `comment`,
  `progress`, `delegate`, `claim`, `done`/`answered`, `reopened`, `reminder`.
- **`outbox`** (PK `id`): messages queued for a busy agent. `surface_uuid`,
  `body`, `frm`, `task_id` (per-task dedup), `status`
  (`pending`/`held`/`delivered`/`canceled`), `delivered`, timestamps. Pending =
  `delivered=0 AND status IN ('pending','held')`; only `pending` is flushed.
- **`chat`** (PK `id`): the message hub timeline. `frm`, `dst`, `body`, `kind`
  (`chat` = human-facing; `report` = operational).
- **`decisions`** (PK `request_id`): Feed decisions. `hook_event`, `tool_name`,
  `disposition` (`auto`/`escalate`), `status` (`pending`→`completed`).
  Idempotent upsert on `request_id`.
- **`events`** (PK `id`): append-only event log (also the usage/turn counter).
- **`goal`** (singleton): the workspace's operating goal.
- **`applied`** (singleton-ish): whether the skill nudge was delivered.

## Must-preserve invariants (the point of the rewrite)

The full mined list (62 items) is the reference; the load-bearing ones:

- **Settle-then-Enter with verify.** After typing into a surface, wait `0.25s`,
  press Enter, re-read the screen, and re-press (≤2×) if the line is still in the
  input. A read failure counts as "submitted" (never spin).
- **Idle-gated, one-per-turn delivery.** Never type into a `working` (or
  shell-running, blocked, errored) surface; queue to the outbox. Deliver exactly
  one queued message per idle turn (track a `flushed_idle` set; clear it when the
  surface leaves idle). Plus a `60s` busy-grace after last activity (bypassed by
  the flush path). This is the de-mixing guarantee.
- **Sender content first, then a `---  — decmux (system) —` separator,** then
  decmux's instructions, so the recipient can tell them apart.
- **Manager human-gate.** Only the human and the bound manager may message
  `you`; a subordinate's `→ you` is rerouted to the manager wrapped as
  `[decmux human-gate | from <x>]`.
- **Downward status withholding.** A status-only manager→subordinate message is
  withheld (logged to the timeline) unless `--force`. The classifier is
  conservative & bilingual (EN/KO): any command signal always delivers; only
  clear status with no command signal is withheld.
- **Classification: screen text — not CPU — is authoritative.** A thinking LLM is
  ~0% CPU yet working. Match the busy spinner **structurally** (`esc to
  interrupt`, `\w+ing…`, live token meter), never by an enumerated verb list.
  Scan only the bottom ~16 lines; bottom-most match wins.
- **6 states** (`working`/`idle`/`stuck`/`error`/`budget`/`blocked-on-decision`,
  plus `dead`) with **hysteresis** (`N` consecutive polls to commit; `budget`/
  `error` commit in 1). Event-stream activity downgrades `stuck`→`idle`.
- **Shell detection via the process tree** (a shell whose parent is the agent
  *runtime* pid = a live Bash tool; the surface's own login shell is excluded),
  plus the hook-stream `PreToolUse(Bash)`→next-tool/Stop/TTL tracker. A live
  shell forces `working` and overrides idle.
- **Self-framing error/limit tokens.** Bare "overloaded" / bare HTTP numbers do
  not match; require an error/status frame. Budget checked before error.
- **Auto-vs-escalate.** Only allowlisted reversible read-type tools auto-answer
  (`once`, never `always`); plan/question/notification always escalate.
- **Reconcile by UUID; never re-spawn a live surface.** Seeding-suppress alerts
  on first poll. Persist the events cursor.
- **Human → `triage`, never auto-filed work.** Only a human message becomes a
  tracked task, landing as `triage` for the manager to judge. `/goal` sets the
  goal (not a task).
- **Auto-close safety net.** An `[AGENT-DONE task #N]` / done-marker report
  closes the task — never from a human, never cross-store, never an already-closed
  task, and only when exactly one candidate matches if no explicit id.
- **cmux-send guard.** decmux-spawned agents get a PATH shim that blocks raw
  `cmux send`/`send-key`/input RPCs and arg'd `respawn-pane`; `DECMUX_REAL_CMUX`
  lets decmux's own calls reach real cmux.

## Open questions

- **Interactive program style:** line-oriented REPL (prompt_toolkit / plain
  async, prints live notifications inline — closest to "CLI 중심, 있는 그대로")
  vs. full-screen TUI (Textual — chat + status + tasks + feed panes). Leaning
  REPL to avoid re-introducing a UI layer. Decide at build time.

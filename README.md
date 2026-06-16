# decmux

A per-workspace, **foreground** control plane for a small team of
[cmux](https://github.com/manaflow-ai/cmux)-hosted AI coding agents. One manager
+ a few workers per project; `decmux` is your single, de-mixed interface to them.

> This is a from-scratch rewrite. The archived original lives at
> [`son-engr-kr/decmux-deprecated`](https://github.com/son-engr-kr/decmux-deprecated).

## Why

Running agents under a top-level LLM "manager" breaks down: the LLM forgets to
poll, stuck agents sit idle, and your keystrokes collide with agents' `cmux send`
on the same TTY. decmux is the deterministic **control plane** (plain code, a real
timer) so the agents can stay the **reasoning plane**.

## Model

- **One per workspace, isolated.** Each cmux workspace (= one project) has its own
  store and session. No shared daemon.
- **Foreground only.** While `decmux` is open it supervises; when you close it,
  nothing runs in the background. (No overnight/unattended mode.)
- **State is durable.** Tasks, chat, assignments, and the queue persist in
  per-workspace SQLite, so closing and reopening remembers everything.
- **`decmux` enters the current workspace.** Run it inside a cmux surface; it
  detects the workspace, reconciles against live agents, and opens the REPL.

## Long-running managing (the headline features)

- **Goal.** `decmux goal "<text>"` sets the workspace goal and briefs the manager
  as operating context (not a task). The manager drives the team toward it.
- **Stuck-handling.** When an agent is `stuck`/`error`/`dead`, decmux
  deterministically **pokes the manager** to intervene (nudge / reassign /
  respawn), and **escalates to you** only if the manager stays silent. No agent
  rots idle while decmux is open.
- **Lean report-up.** A worker's news to the manager (a `--to manager` send, or
  `task done/comment/answer`) is kept in the durable store and shown to the manager
  as a one-line pointer, batched into a single `[decmux · N team updates]` digest —
  not dumped into its context. The manager pulls detail on demand with
  `decmux task show <id>` / `decmux report`. Its context stays lean over long runs,
  unlike a raw in-session subagent that returns its full result into the parent.
  A worker's question / decision / block is exempt — delivered in full and promptly.
- **Workforce lifecycle.** Agents are hired with a term: `decmux spawn --term
  short|long|full` (short = one task, long = a work-stream, full = permanent), and
  optionally in an isolated git worktree (`--worktree`) for parallel exploration.
  decmux **auto-reaps** a *self-spawned* agent once it is idle with its work closed —
  archiving its transcript to `files/archive/` first — and `git worktree remove`s
  its tree. A **human-spawned** agent is never auto-closed; decmux asks you, and you
  release it with `decmux despawn <agent>` (graceful: it wraps up and hands off,
  then closes). Hire → work → reap → re-hire, automatically.
- **Momentum.** When the team coasts (idle, goal unfinished, no open work) decmux
  nudges the manager **once** to advance — pick the next step, spin up short-term
  workers, use worktrees for parallel directions — then backs off (re-armed only
  when the team is busy again, with a cooldown floor). Tight, never a repeated nag.

## Install

```sh
uv tool install git+https://github.com/son-engr-kr/decmux
```

## Use

```sh
decmux setup           # once: install the Claude SessionStart hook (global)
decmux                 # open the REPL for this workspace (supervises in the background)
decmux register        # bind the current surface as this workspace's manager
decmux agent           # in a surface you opened: become a managed agent (instead of `claude`)
decmux spawn --name x  # or: create a managed agent in its own new surface
decmux goal "ship v1"  # set the goal; briefs the manager
decmux status          # agent states    decmux ls   # known workspaces
decmux run             # headless supervision (no REPL)
```

The REPL has a persistent bottom prompt with a live status toolbar (agent
counts, open tasks, goal, and the **next proactive wakeup** — `next:<what> Nm
(HH:MM)`, the minutes until the loop next acts on its own) and tab-completion of
commands and agent names. Type to
message the current target; manager→you messages and state transitions stream in
live *above* the prompt. `/help` lists commands (`/to`, `/status`, `/tasks`,
`/feed`, `/report`, `/goal`, `/quit`).

Agents (and scripts) route through decmux instead of raw `cmux send`:

```sh
decmux send "looked at the logs, root cause is X" --to manager
decmux task done 12 "fixed and verified"
```

Run `decmux setup` once to install a Claude `SessionStart` hook. The hook injects
the decmux protocol into a session's context — but **only for decmux-managed
surfaces** (a spawned agent carries `DECMUX_ROLE`; a registered manager is found
in its workspace store), so your normal Claude sessions are untouched. There is
**no skill file**. decmux-spawned agents also get a PATH guard that blocks raw
`cmux` input. `decmux` and `decmux run` never write global config — only
`decmux setup` does.

To onboard a worker you start yourself, run **`decmux agent`** in that surface
instead of `claude` (or `decmux spawn` to create one in a new surface) — it tags
the surface so the protocol is injected. A bare `claude` is deliberately left
untouched, so a plain Claude session you open in the same workspace never gets
"you are a decmux agent."

## Data & uninstall

Your per-workspace state lives in `~/.local/state/decmux/<workspace-uuid>/`
(SQLite: tasks, chat, goals, agent state; plus `files/`). The only installed
global artifacts are the `SessionStart` hook in `~/.claude/settings.json` and the
on-demand cmux-send guard in `~/.local/share/decmux/bin/`.

```sh
decmux teardown           # one shot: global hook + guard + ALL data + the command

# or piecemeal:
decmux setup --remove     # remove the global SessionStart hook from ~/.claude
decmux purge              # delete this workspace's data   (--all for every workspace)
uv tool uninstall decmux  # remove the command itself
```

`setup` ↔ `setup --remove` owns the global hook; `purge` owns data; `uv` owns the
binary. Even if you skip `setup --remove`, the hook self-guards
(`command -v decmux ... || true`), so it is an inert no-op once decmux is gone.

## Develop

```sh
uv run --with pytest pytest        # tests
```

Design notes: [`DESIGN.md`](DESIGN.md).

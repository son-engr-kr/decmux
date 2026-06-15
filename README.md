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

## Install

```sh
uv tool install git+https://github.com/son-engr-kr/decmux
```

## Use

```sh
decmux setup           # once: install the agent skill + SessionStart hook (global)
decmux                 # open the REPL for this workspace (supervises in the background)
decmux register        # bind the current surface as this workspace's manager
decmux goal "ship v1"  # set the goal; briefs the manager
decmux status          # agent states    decmux ls   # known workspaces
decmux run             # headless supervision (no REPL)
```

The REPL has a persistent bottom prompt with a live status toolbar (agent
counts, open tasks, goal) and tab-completion of commands and agent names. Type to
message the current target; manager→you messages and state transitions stream in
live *above* the prompt. `/help` lists commands (`/to`, `/status`, `/tasks`,
`/feed`, `/report`, `/goal`, `/quit`).

Agents (and scripts) route through decmux instead of raw `cmux send`:

```sh
decmux send "looked at the logs, root cause is X" --to manager
decmux task done 12 "fixed and verified"
```

Run `decmux setup` once to install the decmux skill + a Claude `SessionStart`
hook (so agents learn the protocol). decmux-spawned agents also get a PATH guard
that blocks raw `cmux` input so nothing bypasses the de-mixed channel. `decmux`
and `decmux run` never write global config themselves — only `decmux setup` does,
and `decmux uninstall` reverses it.

## Data & uninstall

Your per-workspace state lives in `~/.local/state/decmux/<workspace-uuid>/`
(SQLite: tasks, chat, goals, agent state; plus `files/`). The installed
integration lives elsewhere: the skill in `~/.claude/skills/decmux/`, a
`SessionStart` hook in `~/.claude/settings.json`, and the cmux-send guard in
`~/.local/share/decmux/bin/`.

```sh
decmux uninstall          # remove the skill + hook + guard, KEEP your data
decmux uninstall --data   # also wipe all per-workspace data
uv tool uninstall decmux  # remove the command itself (leaves data + config)
```

## Develop

```sh
uv run --with pytest pytest        # tests
```

Design notes: [`DESIGN.md`](DESIGN.md).

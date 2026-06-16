"""SQLite runtime store — decmux's single source of truth for one workspace.

Each workspace gets its own store under
``~/.local/state/decmux/<workspace_uuid>/store.db`` (attachments alongside in
``files/``). Because a store *is* a workspace, the old ws/workspace_uuid scoping
columns are gone. State is keyed by stable surface UUID; a restart loads prior
state and prunes surfaces no longer present (reconcile).

Persistence is the whole point of the foreground model: closing and reopening
decmux must remember tasks, chat, assignments, the outbox, and the goal.
"""

from __future__ import annotations

import mimetypes
import os
import re
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

DB_ROOT = Path.home() / ".local" / "state" / "decmux"


def _root() -> Path:
    """State root, overridable via DECMUX_STATE_DIR (resolved at call time)."""
    return Path(os.environ.get("DECMUX_STATE_DIR", str(DB_ROOT)))

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_state (
    surface_uuid   TEXT PRIMARY KEY,
    surface_ref    TEXT,
    title          TEXT,
    state          TEXT,
    last_active    REAL,
    model          TEXT,
    effort         TEXT,
    procs          TEXT,
    note           TEXT,
    busy_kind      TEXT,
    updated_at     REAL
);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    surface_uuid TEXT, title TEXT,
    from_state TEXT, to_state TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS binding (
    role TEXT PRIMARY KEY,
    surface_uuid TEXT, surface_ref TEXT, cwd TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT, payload TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS decisions (
    request_id TEXT PRIMARY KEY, kind TEXT,
    hook_event TEXT, tool_name TEXT, disposition TEXT, status TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, frm TEXT, dst TEXT, body TEXT, kind TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS managed (
    surface_uuid TEXT PRIMARY KEY, role TEXT, kind TEXT,
    term TEXT DEFAULT 'short', origin TEXT DEFAULT 'self',
    status TEXT DEFAULT 'active', ts REAL);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, body TEXT, to_whom TEXT,
    status TEXT, progress TEXT, result TEXT, updated_at REAL,
    source TEXT, author TEXT, assignee TEXT, source_id TEXT,
    delivered INTEGER DEFAULT 0, delivered_at REAL,
    last_reminded_at REAL, reminder_count INTEGER DEFAULT 0,
    escalated_at REAL, closed_at REAL
);
CREATE TABLE IF NOT EXISTS task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER, ts REAL, author TEXT, kind TEXT, body TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, surface_uuid TEXT, surface_ref TEXT,
    frm TEXT, body TEXT, task_id INTEGER,
    delivered INTEGER DEFAULT 0, delivered_at REAL,
    status TEXT DEFAULT 'pending', updated_at REAL
);
"""

# Pending outbox predicate, shared by every query (COALESCE covers rows written
# before a status column existed).
_PENDING = "delivered=0 AND COALESCE(status,'pending') IN ('pending','held')"
_CLOSED = "('done','answered')"


class Store:
    def __init__(self, workspace_uuid: str = "default", *, root: Path | str | None = None) -> None:
        root = Path(root) if root is not None else _root()
        self.workspace_uuid = workspace_uuid
        self.dir = root / workspace_uuid
        self.files_dir = self.dir / "files"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.dir / "store.db")
        self.db.row_factory = sqlite3.Row
        # WAL + a busy timeout let the REPL's threads each hold their own Store
        # connection to the same file (supervision writes, input writes, the live
        # feed reads) without "database is locked" errors. Each thread must use its
        # own Store instance (sqlite connections are not shared across threads).
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=3000")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Additive column migrations for stores created before a column existed."""
        for table, col, decl in [("managed", "kind", "TEXT"),
                                 ("managed", "term", "TEXT DEFAULT 'short'"),
                                 ("managed", "origin", "TEXT DEFAULT 'self'"),
                                 ("managed", "status", "TEXT DEFAULT 'active'"),
                                 ("outbox", "digest", "INTEGER DEFAULT 0")]:
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    # --- meta (singletons: goal, applied flag) ---
    def set_meta(self, key: str, value: str, now=None) -> None:
        now = now if now is not None else time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO meta (key, value, updated_at) VALUES (?,?,?)",
            (key, value, now),
        )

    def get_meta(self, key: str, default: str = "") -> str:
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_goal(self, text: str, now=None) -> None:
        self.set_meta("goal", text, now=now)

    def get_goal(self) -> str:
        return self.get_meta("goal", "")

    # --- managed surfaces (decmux only supervises surfaces it onboarded) ---
    def mark_managed(self, surface_uuid: str, role: str = "agent",
                     kind: str = "claude", term: str = "short",
                     origin: str = "self", now=None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO managed"
            " (surface_uuid, role, kind, term, origin, status, ts) VALUES (?,?,?,?,?,'active',?)",
            (surface_uuid, role, kind, term, origin, now if now is not None else time.time()))

    def unmark_managed(self, surface_uuid: str) -> None:
        self.db.execute("DELETE FROM managed WHERE surface_uuid=?", (surface_uuid,))

    def managed_set(self) -> set[str]:
        return {r["surface_uuid"] for r in self.db.execute("SELECT surface_uuid FROM managed")}

    def managed_rows(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT surface_uuid, role, kind, term, origin, status, ts FROM managed")]

    def managed_row(self, surface_uuid: str) -> dict | None:
        r = self.db.execute(
            "SELECT surface_uuid, role, kind, term, origin, status, ts FROM managed"
            " WHERE surface_uuid=?", (surface_uuid,)).fetchone()
        return dict(r) if r else None

    def set_managed_status(self, surface_uuid: str, status: str) -> None:
        assert status in ("active", "releasing"), f"invalid managed status: {status}"
        self.db.execute("UPDATE managed SET status=? WHERE surface_uuid=?",
                        (status, surface_uuid))

    def managed_kinds(self) -> dict[str, str]:
        return {r["surface_uuid"]: (r["kind"] or "")
                for r in self.db.execute("SELECT surface_uuid, kind FROM managed")}

    def is_managed(self, surface_uuid: str) -> bool:
        return bool(self.db.execute(
            "SELECT 1 FROM managed WHERE surface_uuid=?", (surface_uuid,)).fetchone())

    # --- agent state ---
    def last_states(self) -> dict[str, str]:
        rows = self.db.execute("SELECT surface_uuid, state FROM agent_state").fetchall()
        return {r["surface_uuid"]: r["state"] for r in rows}

    def upsert_state(self, *, surface_uuid, surface_ref, title, state,
                     last_active=None, model=None, effort=None, procs=None,
                     busy_kind=None, now=None) -> None:
        now = now if now is not None else time.time()
        # model/effort are sticky (keep the last detected value if this poll is
        # blank); state/procs/last_active/busy_kind are overwritten each poll.
        self.db.execute(
            """INSERT INTO agent_state
                 (surface_uuid, surface_ref, title, state, last_active,
                  model, effort, procs, busy_kind, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(surface_uuid) DO UPDATE SET
                 surface_ref=excluded.surface_ref, title=excluded.title,
                 state=excluded.state, last_active=excluded.last_active,
                 procs=excluded.procs,
                 model=COALESCE(excluded.model, agent_state.model),
                 effort=COALESCE(excluded.effort, agent_state.effort),
                 busy_kind=excluded.busy_kind,
                 updated_at=excluded.updated_at""",
            (surface_uuid, surface_ref, title, state, last_active,
             model, effort, procs, busy_kind, now),
        )

    def busy_kind_by_surface(self) -> dict[str, str]:
        rows = self.db.execute(
            "SELECT surface_uuid, busy_kind FROM agent_state WHERE busy_kind IS NOT NULL"
        ).fetchall()
        return {r["surface_uuid"]: r["busy_kind"] for r in rows}

    def set_note(self, surface_uuid: str, note: str) -> None:
        self.db.execute("UPDATE agent_state SET note=? WHERE surface_uuid=?",
                        (note, surface_uuid))

    def agent_by_uuid(self, surface_uuid: str) -> dict | None:
        r = self.db.execute(
            "SELECT * FROM agent_state WHERE surface_uuid=?", (surface_uuid,)
        ).fetchone()
        return dict(r) if r else None

    def agent_by_ref(self, surface_ref: str) -> dict | None:
        r = self.db.execute(
            "SELECT surface_uuid, state, last_active, updated_at FROM agent_state"
            " WHERE surface_ref=?",
            (surface_ref,),
        ).fetchone()
        return dict(r) if r else None

    def list_agents(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT surface_uuid, surface_ref, title, state, busy_kind,"
            " last_active, model, effort, note FROM agent_state"
        ).fetchall()
        return [dict(r) for r in rows]

    def prune_absent(self, present: set[str]) -> None:
        rows = self.db.execute("SELECT surface_uuid FROM agent_state").fetchall()
        for u in (r["surface_uuid"] for r in rows if r["surface_uuid"] not in present):
            self.db.execute("DELETE FROM agent_state WHERE surface_uuid=?", (u,))

    # --- transitions ---
    def record_transition(self, *, surface_uuid, title, from_state, to_state, now=None) -> None:
        now = now if now is not None else time.time()
        self.db.execute(
            "INSERT INTO transitions (surface_uuid, title, from_state, to_state, ts)"
            " VALUES (?,?,?,?,?)",
            (surface_uuid, title, from_state, to_state, now),
        )

    def recent_transitions(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM transitions ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def last_transition_id(self) -> int:
        return int(self.db.execute(
            "SELECT COALESCE(MAX(id),0) AS m FROM transitions").fetchone()["m"])

    def transitions_after(self, after_id: int, limit: int = 100) -> list[dict]:
        """New transition rows with id > after_id, oldest first (for a live tail)."""
        rows = self.db.execute(
            "SELECT * FROM transitions WHERE id>? ORDER BY id LIMIT ?", (after_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- manager binding (one manager per workspace) ---
    def bind_manager(self, *, surface_uuid, surface_ref, cwd, now=None) -> None:
        now = now if now is not None else time.time()
        self.db.execute(
            """INSERT INTO binding (role, surface_uuid, surface_ref, cwd, updated_at)
               VALUES ('manager', ?, ?, ?, ?)
               ON CONFLICT(role) DO UPDATE SET
                 surface_uuid=excluded.surface_uuid, surface_ref=excluded.surface_ref,
                 cwd=excluded.cwd, updated_at=excluded.updated_at""",
            (surface_uuid, surface_ref, cwd, now),
        )

    def manager(self):
        """The bound manager as (surface_uuid, surface_ref), or None."""
        r = self.db.execute(
            "SELECT surface_uuid, surface_ref FROM binding WHERE role='manager'"
        ).fetchone()
        return (r["surface_uuid"], r["surface_ref"]) if r else None

    def clear_manager(self) -> None:
        self.db.execute("DELETE FROM binding WHERE role='manager'")

    def is_manager(self, surface_uuid: str) -> bool:
        m = self.manager()
        return bool(m and m[0] == surface_uuid)

    def reassign_manager_work(self, *, old_surface_uuid: str, old_surface_ref: str,
                              new_surface_uuid: str, new_surface_ref: str, now=None) -> dict:
        """Move pending manager outbox + requeue open manager tasks to a new surface."""
        now = now if now is not None else time.time()
        moved = self.db.execute(
            f"""UPDATE outbox SET surface_uuid=?, surface_ref=?, updated_at=?
                WHERE {_PENDING} AND (surface_uuid=? OR surface_ref=?)""",
            (new_surface_uuid, new_surface_ref, now, old_surface_uuid, old_surface_ref),
        ).rowcount
        rows = self.db.execute(
            f"SELECT id FROM tasks WHERE lower(to_whom)='manager' AND status NOT IN {_CLOSED}"
        ).fetchall()
        task_ids = [int(r["id"]) for r in rows]
        if task_ids:
            marks = ",".join("?" for _ in task_ids)
            self.db.execute(
                f"""UPDATE tasks SET delivered=0, delivered_at=NULL,
                        last_reminded_at=NULL, updated_at=? WHERE id IN ({marks})""",
                [now, *task_ids],
            )
            for tid in task_ids:
                self.add_task_comment(
                    tid, author="decmux", kind="reassign",
                    body=f"manager changed to {new_surface_ref}; task requeued", now=now)
        return {"moved_outbox": moved, "requeued_tasks": len(task_ids)}

    # --- task queue (chat commands/questions tracked like issues) ---
    def add_task(self, *, kind, body, to_whom="manager", source="chat",
                 author="human", source_id="", status="open", now=None) -> int:
        now = now if now is not None else time.time()
        if source_id:
            found = self.find_task_by_source_id(source_id)
            if found:
                return found["id"]
        cur = self.db.execute(
            "INSERT INTO tasks (ts, kind, body, to_whom, status, progress, result,"
            " updated_at, source, author, source_id, assignee, delivered, reminder_count)"
            " VALUES (?,?,?,?,?,'','',?,?,?,?, '', 0, 0)",
            (now, kind, body, to_whom, status, now, source, author, source_id),
        )
        tid = cur.lastrowid
        self.add_task_comment(tid, author=author, kind="created", body=body, now=now)
        return tid

    def add_task_comment(self, tid: int, *, author: str, body: str,
                         kind: str = "comment", now=None) -> int:
        now = now if now is not None else time.time()
        assert self.get_task(tid), f"task #{tid} not found"
        cur = self.db.execute(
            "INSERT INTO task_comments (task_id, ts, author, kind, body) VALUES (?,?,?,?,?)",
            (tid, now, author, kind, body),
        )
        self.db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now, tid))
        return cur.lastrowid

    def task_progress(self, tid: int, text: str, author="manager", now=None) -> None:
        now = now if now is not None else time.time()
        self.db.execute(
            "UPDATE tasks SET progress=COALESCE(progress,'')||?, status='in_progress',"
            " updated_at=? WHERE id=?",
            (f"• {text}\n", now, tid),
        )
        self.add_task_comment(tid, author=author, kind="progress", body=text, now=now)

    def delegate_task(self, tid: int, assignee: str, instruction: str,
                      author="manager", now=None) -> None:
        now = now if now is not None else time.time()
        self.db.execute(
            "UPDATE tasks SET assignee=?, progress=COALESCE(progress,'')||?,"
            " status='in_progress', updated_at=? WHERE id=?",
            (assignee, f"• delegated to {assignee}: {instruction}\n", now, tid),
        )
        self.add_task_comment(tid, author=author, kind="delegate",
                              body=f"{assignee}: {instruction}", now=now)

    def claim_task(self, tid: int, assignee: str, now=None) -> None:
        now = now if now is not None else time.time()
        self.db.execute(
            "UPDATE tasks SET assignee=?, status='in_progress', updated_at=? WHERE id=?",
            (assignee, now, tid),
        )
        self.add_task_comment(tid, author=assignee, kind="claim",
                              body=f"claimed by {assignee}", now=now)

    def close_task(self, tid: int, result: str, status="done", author="manager",
                   now=None) -> None:
        now = now if now is not None else time.time()
        self.db.execute(
            "UPDATE tasks SET status=?, result=?, updated_at=?, closed_at=? WHERE id=?",
            (status, result, now, now, tid))
        self.add_task_comment(tid, author=author, kind=status, body=result, now=now)

    def reopen_task(self, tid: int, author="manager", now=None) -> None:
        now = now if now is not None else time.time()
        self.db.execute(
            "UPDATE tasks SET status='open', closed_at=NULL, updated_at=? WHERE id=?",
            (now, tid))
        self.add_task_comment(tid, author=author, kind="reopened",
                              body="task reopened", now=now)

    def increment_task_delivered(self, tid: int, count: int = 1, now=None) -> None:
        if count <= 0:
            return
        now = now if now is not None else time.time()
        self.db.execute(
            "UPDATE tasks SET delivered=COALESCE(delivered,0)+?,"
            " delivered_at=COALESCE(delivered_at,?), updated_at=? WHERE id=?",
            (count, now, now, tid),
        )

    def mark_task_reminded(self, tid: int, *, author="decmux", body="", now=None) -> None:
        now = now if now is not None else time.time()
        self.db.execute(
            "UPDATE tasks SET last_reminded_at=?, reminder_count=COALESCE(reminder_count,0)+1,"
            " updated_at=? WHERE id=?",
            (now, now, tid),
        )
        if body:
            self.add_task_comment(tid, author=author, kind="reminder", body=body, now=now)

    def mark_task_escalated(self, tid: int, *, author="decmux", body="", now=None) -> None:
        now = now if now is not None else time.time()
        self.db.execute("UPDATE tasks SET escalated_at=?, updated_at=? WHERE id=?",
                        (now, now, tid))
        if body:
            self.add_task_comment(tid, author=author, kind="escalation", body=body, now=now)

    def get_task(self, tid: int) -> dict:
        r = self.db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        assert r, f"task #{tid} not found"
        return dict(r)

    def find_task_by_source_id(self, source_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM tasks WHERE source_id=?", (source_id,)).fetchone()
        return dict(r) if r else None

    def task_comments(self, tid: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM task_comments WHERE task_id=? ORDER BY id", (tid,)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_tasks(self, limit: int = 60, include_comments: bool = False) -> list[dict]:
        rows = self.db.execute(
            f"SELECT * FROM tasks ORDER BY (status IN {_CLOSED}), id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        tasks = [dict(r) for r in rows]
        if include_comments:
            for t in tasks:
                t["comments"] = self.task_comments(t["id"])
        return tasks

    def open_tasks(self, limit: int = 80) -> list[dict]:
        rows = self.db.execute(
            f"SELECT * FROM tasks WHERE status NOT IN {_CLOSED} ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def open_tasks_for_actor(self, *, actor: str, limit: int = 5) -> list[dict]:
        actor_l = actor.lower()
        rows = self.db.execute(
            f"""SELECT * FROM tasks
               WHERE status NOT IN {_CLOSED}
                 AND (lower(to_whom)=? OR lower(assignee)=?
                      OR (?='manager' AND lower(to_whom)='manager'))
               ORDER BY updated_at DESC, id DESC LIMIT ?""",
            (actor_l, actor_l, actor_l, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- chat / message hub ---
    def add_chat(self, *, frm, dst, body, kind, now=None) -> None:
        now = now if now is not None else time.time()
        self.db.execute(
            "INSERT INTO chat (ts, frm, dst, body, kind) VALUES (?,?,?,?,?)",
            (now, frm, dst, body, kind),
        )

    def recent_chat(self, limit: int = 150, kind: str | None = None) -> list[dict]:
        # kind='chat' -> human-facing conversation; 'report' -> operational; None -> all.
        where = "WHERE kind=?" if kind else ""
        params: list = [kind] if kind else []
        params.append(limit)
        rows = self.db.execute(
            f"SELECT id, ts, frm, dst, body, kind FROM chat {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def last_chat_id(self) -> int:
        return int(self.db.execute("SELECT COALESCE(MAX(id),0) AS m FROM chat").fetchone()["m"])

    def chat_after(self, after_id: int, kind: str | None = None, limit: int = 100) -> list[dict]:
        """New chat rows with id > after_id, oldest first (for a live tail)."""
        where = "WHERE id>?"
        params: list = [after_id]
        if kind:
            where += " AND kind=?"
            params.append(kind)
        params.append(limit)
        rows = self.db.execute(
            f"SELECT id, ts, frm, dst, body, kind FROM chat {where} ORDER BY id LIMIT ?", params,
        ).fetchall()
        return [dict(r) for r in rows]

    # --- file attachments (on disk under this workspace's files/) ---
    def save_file(self, *, data: bytes, name: str) -> dict:
        self.files_dir.mkdir(parents=True, exist_ok=True)
        ext = os.path.splitext(name)[1]
        fid = uuid4().hex + ext
        (self.files_dir / fid).write_bytes(data)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return {"id": fid, "name": name, "mime": mime, "size": len(data)}

    def archive_transcript(self, name: str, text: str) -> str:
        """Save a released agent's screen transcript under files/archive/ before its
        surface is closed, so its context isn't lost when it is reaped."""
        d = self.files_dir / "archive"
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.-]", "_", name or "")[:40] or "agent"
        path = d / f"{safe}-{int(time.time())}.txt"
        path.write_text(text or "")
        return str(path)

    def file_abspath(self, fid: str) -> str | None:
        """Resolve a stored-file id to its absolute path, refusing traversal."""
        if not fid or "/" in fid or "\\" in fid or ".." in fid:
            return None
        path = os.path.realpath(self.files_dir / fid)
        base = os.path.realpath(self.files_dir)
        if path != base and not path.startswith(base + os.sep):
            return None
        return path if os.path.isfile(path) else None

    def read_file(self, fid: str) -> bytes | None:
        path = self.file_abspath(fid)
        return Path(path).read_bytes() if path else None

    # --- outbox: messages queued for a busy agent, flushed when it goes idle ---
    def has_pending_outbox(self, *, surface_uuid: str, task_id: int) -> bool:
        row = self.db.execute(
            f"SELECT 1 FROM outbox WHERE surface_uuid=? AND task_id=? AND {_PENDING}",
            (surface_uuid, task_id),
        ).fetchone()
        return bool(row)

    def task_has_pending_delivery(self, tid: int) -> bool:
        row = self.db.execute(
            f"SELECT 1 FROM outbox WHERE task_id=? AND {_PENDING}", (tid,),
        ).fetchone()
        return bool(row)

    def task_pending_delivery_count(self, tid: int) -> int:
        row = self.db.execute(
            f"SELECT COUNT(*) AS n FROM outbox WHERE task_id=? AND {_PENDING}", (tid,),
        ).fetchone()
        return int(row["n"] or 0)

    def enqueue_outbox(self, *, surface_uuid, surface_ref, body, frm="",
                       task_id: int | None = None, digest: bool = False, now=None) -> int:
        now = now if now is not None else time.time()
        if task_id is not None and self.has_pending_outbox(
            surface_uuid=surface_uuid, task_id=task_id
        ):
            return 0
        cur = self.db.execute(
            "INSERT INTO outbox"
            " (ts, surface_uuid, surface_ref, frm, body, task_id, digest, delivered, status,"
            "  updated_at) VALUES (?,?,?,?,?,?,?,0,'pending',?)",
            (now, surface_uuid, surface_ref, frm, body, task_id, 1 if digest else 0, now),
        )
        return cur.lastrowid

    def pending_outbox(self, surface_uuid: str, limit: int = 10) -> list[dict]:
        """Queued (status='pending') messages for one surface, FIFO. Does not mark.

        Strictly 'pending' — a 'held' message is counted for dedup but never flushed.
        """
        return [dict(r) for r in self.db.execute(
            "SELECT id, body, task_id, COALESCE(digest,0) AS digest FROM outbox"
            " WHERE surface_uuid=? AND delivered=0 AND COALESCE(status,'pending')='pending'"
            " ORDER BY id LIMIT ?",
            (surface_uuid, limit),
        )]

    def mark_outbox_delivered(self, ids: list[int], now=None) -> None:
        if not ids:
            return
        now = now if now is not None else time.time()
        self.db.executemany(
            "UPDATE outbox SET delivered=1, delivered_at=?, status='delivered',"
            " updated_at=? WHERE id=?",
            [(now, now, i) for i in ids],
        )

    def outbox_counts(self) -> dict:
        return {r["surface_uuid"]: r["n"] for r in self.db.execute(
            f"SELECT surface_uuid, COUNT(*) AS n FROM outbox WHERE {_PENDING}"
            " GROUP BY surface_uuid"
        )}

    def list_outbox(self) -> list[dict]:
        rows = self.db.execute(
            f"""SELECT o.id, o.ts, o.surface_uuid, o.surface_ref, o.frm, o.body,
                      o.task_id, COALESCE(o.status,'pending') AS status, o.updated_at,
                      a.title AS target, a.state AS target_state
               FROM outbox o
               LEFT JOIN agent_state a ON a.surface_uuid=o.surface_uuid
               WHERE o.delivered=0 AND COALESCE(o.status,'pending') IN ('pending','held')
               ORDER BY o.id"""
        ).fetchall()
        return [dict(r) for r in rows]

    def update_outbox(self, oid: int, *, body: str | None = None,
                      status: str | None = None, now=None) -> None:
        now = now if now is not None else time.time()
        row = self.db.execute("SELECT delivered FROM outbox WHERE id=?", (oid,)).fetchone()
        assert row, f"queued message #{oid} not found"
        assert not row["delivered"], f"queued message #{oid} already delivered"
        sets = ["updated_at=?"]
        args: list = [now]
        if body is not None:
            sets.append("body=?")
            args.append(body)
        if status is not None:
            assert status in ("pending", "held", "canceled"), f"invalid outbox status: {status}"
            sets.append("status=?")
            args.append(status)
            if status == "canceled":
                sets.append("delivered=1")
                sets.append("delivered_at=?")
                args.append(now)
        args.append(oid)
        self.db.execute(f"UPDATE outbox SET {', '.join(sets)} WHERE id=?", args)

    # --- events / usage ---
    def log_event(self, *, kind, payload="", now=None) -> None:
        now = now if now is not None else time.time()
        self.db.execute(
            "INSERT INTO events (kind, payload, ts) VALUES (?,?,?)", (kind, payload, now),
        )

    def recent_events(self, limit: int = 30) -> list[dict]:
        rows = self.db.execute(
            "SELECT kind, ts FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def usage(self) -> dict:
        """Workspace activity from the event log (turns/tools/active span)."""
        r = self.db.execute(
            """SELECT SUM(kind='agent.hook.Stop')      AS turns,
                      SUM(kind='agent.hook.PreToolUse') AS tools,
                      MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS events
               FROM events"""
        ).fetchone()
        return dict(r)

    # --- feed decisions ---
    def add_decision(self, *, request_id, kind, hook_event, tool_name,
                     disposition, status="pending", now=None) -> None:
        now = now if now is not None else time.time()
        self.db.execute(
            """INSERT INTO decisions
                 (request_id, kind, hook_event, tool_name, disposition, status, ts)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(request_id) DO UPDATE SET
                 disposition=excluded.disposition, status=excluded.status""",
            (request_id, kind, hook_event, tool_name, disposition, status, now),
        )

    def resolve_decision(self, request_id: str, status: str) -> None:
        self.db.execute("UPDATE decisions SET status=? WHERE request_id=?", (status, request_id))

    def list_decisions(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.db.execute(
                "SELECT * FROM decisions WHERE status=? ORDER BY ts DESC", (status,)
            ).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM decisions ORDER BY ts DESC LIMIT 50").fetchall()
        return [dict(r) for r in rows]

    def commit(self) -> None:
        self.db.commit()

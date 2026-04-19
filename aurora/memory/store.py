"""SQLite-backed persistent memory: conversation history + solution knowledge base."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT 'New conversation',
    model       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    pinned      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,          -- user | assistant | tool
    content         TEXT NOT NULL DEFAULT '',
    blocks_json     TEXT,                   -- JSON list of content blocks (tool calls etc.)
    thinking        TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    response_time_ms REAL NOT NULL DEFAULT 0,
    model           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS solutions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL DEFAULT '',
    problem         TEXT NOT NULL,
    solution        TEXT NOT NULL,
    tags            TEXT NOT NULL DEFAULT '[]',  -- JSON array
    source_conv_id  TEXT,
    created_at      TEXT NOT NULL,
    last_used_at    TEXT,
    use_count       INTEGER NOT NULL DEFAULT 0
);

-- Full-text search index for solutions
CREATE VIRTUAL TABLE IF NOT EXISTS solutions_fts USING fts5(
    problem, solution, title, tags,
    content=solutions,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS solutions_ai AFTER INSERT ON solutions BEGIN
    INSERT INTO solutions_fts(rowid, problem, solution, title, tags)
    VALUES (new.id, new.problem, new.solution, new.title, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS solutions_ad AFTER DELETE ON solutions BEGIN
    INSERT INTO solutions_fts(solutions_fts, rowid, problem, solution, title, tags)
    VALUES ('delete', old.id, old.problem, old.solution, old.title, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS solutions_au AFTER UPDATE ON solutions BEGIN
    INSERT INTO solutions_fts(solutions_fts, rowid, problem, solution, title, tags)
    VALUES ('delete', old.id, old.problem, old.solution, old.title, old.tags);
    INSERT INTO solutions_fts(rowid, problem, solution, title, tags)
    VALUES (new.id, new.problem, new.solution, new.title, new.tags);
END;

CREATE INDEX IF NOT EXISTS idx_messages_conv  ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_conv_updated   ON conversations(updated_at DESC);
"""

_store: Optional["MemoryStore"] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class MemoryStore:
    def __init__(self, db_path: str = "~/.local/share/aurora/memory.db"):
        self.db_path = str(Path(db_path).expanduser())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_DDL)
            # Migration: add response_time_ms column if it doesn't exist
            try:
                await db.execute(
                    "ALTER TABLE messages ADD COLUMN response_time_ms REAL NOT NULL DEFAULT 0"
                )
            except aiosqlite.OperationalError:
                pass  # column already exists
            await db.commit()

    # ─── Conversations ─────────────────────────────────────────────────────

    async def create_conversation(self, title: str = "New conversation", model: str = "") -> str:
        cid = str(uuid.uuid4())
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
                (cid, title, model, now, now),
            )
            await db.commit()
        return cid

    async def update_conversation(self, cid: str, title: str | None = None) -> None:
        if title is None:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                (title, _now(), cid),
            )
            await db.commit()

    async def list_conversations(self, limit: int = 50) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, title, model, created_at, updated_at, pinned FROM conversations "
                "ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_conversation(self, cid: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM conversations WHERE id=?", (cid,))
            await db.commit()

    # ─── Messages ──────────────────────────────────────────────────────────

    async def add_message(
        self,
        cid: str,
        role: str,
        content: str,
        blocks: list[dict] | None = None,
        thinking: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = "",
        response_time_ms: float = 0,
    ) -> int:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """INSERT INTO messages
                   (conversation_id, role, content, blocks_json, thinking,
                    input_tokens, output_tokens, model, response_time_ms, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    cid, role, content,
                    json.dumps(blocks) if blocks else None,
                    thinking, input_tokens, output_tokens, model, response_time_ms, now,
                ),
            )
            await db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (now, cid)
            )
            await db.commit()
            return cur.lastrowid  # type: ignore[return-value]

    async def get_messages(self, cid: str) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY id",
                (cid,),
            )
            rows = await cur.fetchall()

        result = []
        for r in rows:
            d = dict(r)
            if d.get("blocks_json"):
                d["blocks"] = json.loads(d["blocks_json"])
            del d["blocks_json"]
            result.append(d)
        return result

    # ─── Solutions / Memory ────────────────────────────────────────────────

    async def save_solution(
        self,
        problem: str,
        solution: str,
        title: str = "",
        tags: list[str] | None = None,
        source_conv_id: str | None = None,
    ) -> int:
        now = _now()
        tags_json = json.dumps(tags or [])
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """INSERT INTO solutions (title, problem, solution, tags, source_conv_id, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (title, problem, solution, tags_json, source_conv_id, now),
            )
            await db.commit()
            return cur.lastrowid  # type: ignore[return-value]

    async def search_solutions(self, query: str, limit: int = 5) -> list[dict]:
        """FTS search with keyword fallback."""
        results: list[dict] = []

        # Clean query for FTS5 (escape special chars)
        fts_query = re.sub(r'[^\w\s]', ' ', query).strip()

        if fts_query:
            try:
                async with aiosqlite.connect(self.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    cur = await db.execute(
                        """SELECT s.* FROM solutions s
                           JOIN solutions_fts f ON s.id = f.rowid
                           WHERE solutions_fts MATCH ?
                           ORDER BY rank LIMIT ?""",
                        (fts_query, limit),
                    )
                    rows = await cur.fetchall()
                    results = [dict(r) for r in rows]
            except Exception:
                pass  # FTS might fail on malformed query — fall through

        # Fallback: simple LIKE search
        if not results:
            keywords = [w for w in fts_query.lower().split() if len(w) > 3][:5]
            if keywords:
                like_clause = " OR ".join(
                    "lower(problem || ' ' || solution) LIKE ?" for _ in keywords
                )
                params = [f"%{k}%" for k in keywords] + [limit]
                async with aiosqlite.connect(self.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    cur = await db.execute(
                        f"SELECT * FROM solutions WHERE {like_clause} ORDER BY use_count DESC LIMIT ?",
                        params,
                    )
                    rows = await cur.fetchall()
                    results = [dict(r) for r in rows]

        # Parse tags JSON
        for r in results:
            if isinstance(r.get("tags"), str):
                try:
                    r["tags"] = json.loads(r["tags"])
                except Exception:
                    r["tags"] = []
        return results

    async def list_solutions(self, limit: int = 50) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM solutions ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            rows = await cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("tags"), str):
                try:
                    d["tags"] = json.loads(d["tags"])
                except Exception:
                    d["tags"] = []
            result.append(d)
        return result

    async def delete_solution(self, sid: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM solutions WHERE id=?", (sid,))
            await db.commit()

    async def bump_solution(self, sid: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE solutions SET use_count=use_count+1, last_used_at=? WHERE id=?",
                (_now(), sid),
            )
            await db.commit()


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        from ..config import get
        cfg = get()
        db_path = getattr(getattr(cfg, "memory", None), "db_path", None) or \
                  "~/.local/share/aurora/memory.db"
        _store = MemoryStore(db_path)
    return _store

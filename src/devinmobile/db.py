import json
import time
from dataclasses import dataclass, field

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings (
    session_id     TEXT PRIMARY KEY,
    thread_id      INTEGER NOT NULL,
    channel_id     INTEGER NOT NULL,
    anchor_msg_id  INTEGER,
    title          TEXT,
    url            TEXT,
    status         TEXT,
    status_detail  TEXT,
    msg_cursor     TEXT,
    seen_event_ids TEXT NOT NULL DEFAULT '[]',
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     INTEGER NOT NULL
);
"""

# How many event ids to keep for replay dedupe.
SEEN_CAP = 500


@dataclass
class Binding:
    session_id: str
    thread_id: int
    channel_id: int
    anchor_msg_id: int | None = None
    title: str | None = None
    url: str | None = None
    status: str | None = None
    status_detail: str | None = None
    msg_cursor: str | None = None
    seen_event_ids: set[str] = field(default_factory=set)
    active: bool = True
    created_at: int = 0


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def connect(cls, path: str) -> "Database":
        conn = await aiosqlite.connect(path)
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    @staticmethod
    def _row_to_binding(row: aiosqlite.Row) -> Binding:
        return Binding(
            session_id=row[0],
            thread_id=row[1],
            channel_id=row[2],
            anchor_msg_id=row[3],
            title=row[4],
            url=row[5],
            status=row[6],
            status_detail=row[7],
            msg_cursor=row[8],
            seen_event_ids=set(json.loads(row[9])),
            active=bool(row[10]),
            created_at=row[11],
        )

    async def upsert_binding(self, b: Binding) -> None:
        if not b.created_at:
            b.created_at = int(time.time())
        await self._conn.execute(
            """INSERT INTO bindings
               (session_id, thread_id, channel_id, anchor_msg_id, title, url,
                status, status_detail, msg_cursor, seen_event_ids, active, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                 thread_id=excluded.thread_id, channel_id=excluded.channel_id,
                 anchor_msg_id=excluded.anchor_msg_id, title=excluded.title,
                 url=excluded.url, status=excluded.status,
                 status_detail=excluded.status_detail, msg_cursor=excluded.msg_cursor,
                 seen_event_ids=excluded.seen_event_ids, active=excluded.active""",
            (
                b.session_id, b.thread_id, b.channel_id, b.anchor_msg_id, b.title, b.url,
                b.status, b.status_detail, b.msg_cursor,
                json.dumps(sorted(b.seen_event_ids)[-SEEN_CAP:]),
                int(b.active), b.created_at,
            ),
        )
        await self._conn.commit()

    async def get_binding_by_thread(self, thread_id: int) -> Binding | None:
        async with self._conn.execute(
            "SELECT * FROM bindings WHERE thread_id = ?", (thread_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_binding(row) if row else None

    async def get_binding(self, session_id: str) -> Binding | None:
        async with self._conn.execute(
            "SELECT * FROM bindings WHERE session_id = ?", (session_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_binding(row) if row else None

    async def active_bindings(self) -> list[Binding]:
        async with self._conn.execute(
            "SELECT * FROM bindings WHERE active = 1 ORDER BY created_at"
        ) as cur:
            return [self._row_to_binding(r) for r in await cur.fetchall()]

    async def all_bindings(self, limit: int = 25) -> list[Binding]:
        async with self._conn.execute(
            "SELECT * FROM bindings ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            return [self._row_to_binding(r) for r in await cur.fetchall()]

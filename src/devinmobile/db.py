import json
import sqlite3
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
    model          TEXT,
    last_msg       TEXT,
    created_at     INTEGER NOT NULL
);

-- One row per (session, PR): tracks the Discord card + last notified state
-- so webhooks and polling converge without double-posting.
CREATE TABLE IF NOT EXISTS prs (
    session_id     TEXT NOT NULL,
    pr_url         TEXT NOT NULL,
    owner          TEXT,
    repo           TEXT,
    number         INTEGER,
    pr_title       TEXT,
    state          TEXT,          -- open|closed|merged (derived)
    checks_state   TEXT,          -- none|pending|success|failure
    card_msg_id    INTEGER,
    last_notified  TEXT,          -- last transition we posted about
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (session_id, pr_url)
);
"""

# Columns added after the initial schema — keyed by column name.
MIGRATIONS = {
    "model": "ALTER TABLE bindings ADD COLUMN model TEXT",
    "last_msg": "ALTER TABLE bindings ADD COLUMN last_msg TEXT",
}

# How many event ids to keep for replay dedupe.
SEEN_CAP = 500


@dataclass
class PrRow:
    session_id: str
    pr_url: str
    owner: str = ""
    repo: str = ""
    number: int = 0
    pr_title: str | None = None
    state: str | None = None
    checks_state: str | None = None
    card_msg_id: int | None = None
    last_notified: str | None = None
    updated_at: int = 0


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
    model: str | None = None
    last_msg: str | None = None  # most recent Devin message text (for ?-detection)
    created_at: int = 0


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def connect(cls, path: str) -> "Database":
        conn = await aiosqlite.connect(path)
        conn.row_factory = sqlite3.Row
        await conn.executescript(SCHEMA)
        async with conn.execute("PRAGMA table_info(bindings)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        for col, ddl in MIGRATIONS.items():
            if col not in cols:
                await conn.execute(ddl)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    @staticmethod
    def _row_to_binding(row: sqlite3.Row) -> Binding:
        return Binding(
            session_id=row["session_id"],
            thread_id=row["thread_id"],
            channel_id=row["channel_id"],
            anchor_msg_id=row["anchor_msg_id"],
            title=row["title"],
            url=row["url"],
            status=row["status"],
            status_detail=row["status_detail"],
            msg_cursor=row["msg_cursor"],
            seen_event_ids=set(json.loads(row["seen_event_ids"])),
            active=bool(row["active"]),
            model=row["model"] if "model" in row.keys() else None,
            last_msg=row["last_msg"] if "last_msg" in row.keys() else None,
            created_at=row["created_at"] or 0,
        )

    async def upsert_binding(self, b: Binding) -> None:
        if not b.created_at:
            b.created_at = int(time.time())
        await self._conn.execute(
            """INSERT INTO bindings
               (session_id, thread_id, channel_id, anchor_msg_id, title, url,
                status, status_detail, msg_cursor, seen_event_ids, active, model,
                last_msg, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                 thread_id=excluded.thread_id, channel_id=excluded.channel_id,
                 anchor_msg_id=excluded.anchor_msg_id, title=excluded.title,
                 url=excluded.url, status=excluded.status,
                 status_detail=excluded.status_detail, msg_cursor=excluded.msg_cursor,
                 seen_event_ids=excluded.seen_event_ids, active=excluded.active,
                 model=excluded.model, last_msg=excluded.last_msg""",
            (
                b.session_id, b.thread_id, b.channel_id, b.anchor_msg_id, b.title, b.url,
                b.status, b.status_detail, b.msg_cursor,
                json.dumps(sorted(b.seen_event_ids)[-SEEN_CAP:]),
                int(b.active), b.model, b.last_msg, b.created_at,
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

    # ---- PR tracking -------------------------------------------------------

    @staticmethod
    def _row_to_pr(row: sqlite3.Row) -> PrRow:
        return PrRow(
            session_id=row["session_id"], pr_url=row["pr_url"],
            owner=row["owner"], repo=row["repo"], number=row["number"],
            pr_title=row["pr_title"], state=row["state"],
            checks_state=row["checks_state"], card_msg_id=row["card_msg_id"],
            last_notified=row["last_notified"], updated_at=row["updated_at"],
        )

    async def upsert_pr(self, pr: PrRow) -> None:
        if not pr.updated_at:
            pr.updated_at = int(time.time())
        await self._conn.execute(
            """INSERT INTO prs
               (session_id, pr_url, owner, repo, number, pr_title, state,
                checks_state, card_msg_id, last_notified, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id, pr_url) DO UPDATE SET
                 -- state-only upserts carry empty identity fields; keep the row's
                 owner=COALESCE(NULLIF(excluded.owner, ''), prs.owner),
                 repo=COALESCE(NULLIF(excluded.repo, ''), prs.repo),
                 number=CASE WHEN excluded.number > 0
                             THEN excluded.number ELSE prs.number END,
                 pr_title=COALESCE(excluded.pr_title, prs.pr_title),
                 state=COALESCE(excluded.state, prs.state),
                 checks_state=COALESCE(excluded.checks_state, prs.checks_state),
                 card_msg_id=COALESCE(excluded.card_msg_id, prs.card_msg_id),
                 last_notified=COALESCE(excluded.last_notified, prs.last_notified),
                 updated_at=excluded.updated_at""",
            (
                pr.session_id, pr.pr_url, pr.owner, pr.repo, pr.number,
                pr.pr_title, pr.state, pr.checks_state, pr.card_msg_id,
                pr.last_notified, pr.updated_at,
            ),
        )
        await self._conn.commit()

    async def get_pr(self, session_id: str, pr_url: str) -> PrRow | None:
        async with self._conn.execute(
            "SELECT * FROM prs WHERE session_id = ? AND pr_url = ?",
            (session_id, pr_url),
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_pr(row) if row else None

    async def prs_for_session(self, session_id: str) -> list[PrRow]:
        async with self._conn.execute(
            "SELECT * FROM prs WHERE session_id = ?", (session_id,)
        ) as cur:
            return [self._row_to_pr(r) for r in await cur.fetchall()]

    async def get_pr_by_number(
        self, session_id: str, number: int
    ) -> PrRow | None:
        async with self._conn.execute(
            "SELECT * FROM prs WHERE session_id = ? AND number = ?",
            (session_id, number),
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_pr(row) if row else None

    async def binding_for_pr(
        self, owner: str, repo: str, number: int
    ) -> tuple[Binding, PrRow] | None:
        """Reverse lookup for webhooks: PR identity → (binding, pr row)."""
        async with self._conn.execute(
            """SELECT b.*, p.* FROM prs p JOIN bindings b
                 ON b.session_id = p.session_id
               WHERE p.owner = ? AND p.repo = ? AND p.number = ?""",
            (owner, repo, number),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        # b.* and p.* share only the join key (session_id) — Row returns the
        # first column of a duplicated name, which is the same value anyway.
        return self._row_to_binding(row), self._row_to_pr(row)

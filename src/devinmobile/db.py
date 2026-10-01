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
    acus           REAL NOT NULL DEFAULT 0,
    acu_warned     INTEGER NOT NULL DEFAULT 0,
    last_activity_at INTEGER,
    quiet_alerted  INTEGER NOT NULL DEFAULT 0,
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
    auto_merge     INTEGER,       -- NULL/0 off, 1 on (NULL keeps upserts neutral)
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (session_id, pr_url)
);

-- Recurring task queue for /schedule — a row fires spawn_session() when due.
CREATE TABLE IF NOT EXISTS schedules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt           TEXT NOT NULL,
    repos            TEXT NOT NULL DEFAULT '[]',  -- json list of owner/repo
    model            TEXT,
    interval_seconds INTEGER NOT NULL,
    next_run_at      INTEGER NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_session_id  TEXT,
    created_at       INTEGER NOT NULL
);
"""

# Columns added after the initial schema — table -> column -> DDL. Adding a
# column: put it in SCHEMA above AND here so existing DBs get the ALTER.
MIGRATIONS: dict[str, dict[str, str]] = {
    "bindings": {
        "model": "ALTER TABLE bindings ADD COLUMN model TEXT",
        "last_msg": "ALTER TABLE bindings ADD COLUMN last_msg TEXT",
        "acus": "ALTER TABLE bindings ADD COLUMN acus REAL NOT NULL DEFAULT 0",
        # bitmask of which ACU-bucket pings already fired (1=80%, 2=100%)
        "acu_warned": (
            "ALTER TABLE bindings ADD COLUMN acu_warned INTEGER NOT NULL DEFAULT 0"
        ),
        "last_activity_at": (
            "ALTER TABLE bindings ADD COLUMN last_activity_at INTEGER"
        ),
        # set once per quiet streak so the watchdog posts once, not every tick
        "quiet_alerted": (
            "ALTER TABLE bindings ADD COLUMN quiet_alerted INTEGER NOT NULL DEFAULT 0"
        ),
    },
    "prs": {
        "auto_merge": (
            "ALTER TABLE prs ADD COLUMN auto_merge INTEGER"
        ),
    },
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
    auto_merge: bool | None = None  # None = upsert shouldn't touch the column
    updated_at: int = 0


@dataclass
class ScheduleRow:
    id: int
    prompt: str
    repos: list[str] = field(default_factory=list)
    model: str | None = None
    interval_seconds: int = 3600
    next_run_at: int = 0
    enabled: bool = True
    last_session_id: str | None = None
    created_at: int = 0


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
    seen_event_ids: list[str] = field(default_factory=list)  # insertion-ordered
    active: bool = True
    model: str | None = None
    last_msg: str | None = None  # most recent Devin message text (for ?-detection)
    acus: float = 0.0
    acu_warned: int = 0  # bitmask: 1 = 80% pinged, 2 = 100% pinged
    last_activity_at: int = 0  # watchdog anchor; 0 = fall back to created_at
    quiet_alerted: bool = False
    created_at: int = 0


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def connect(cls, path: str) -> "Database":
        conn = await aiosqlite.connect(path)
        conn.row_factory = sqlite3.Row
        await conn.executescript(SCHEMA)
        for table, cols_ddl in MIGRATIONS.items():
            async with conn.execute(f"PRAGMA table_info({table})") as cur:
                cols = {r[1] for r in await cur.fetchall()}
            for col, ddl in cols_ddl.items():
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
            seen_event_ids=list(json.loads(row["seen_event_ids"])),
            active=bool(row["active"]),
            model=row["model"] if "model" in row.keys() else None,
            last_msg=row["last_msg"] if "last_msg" in row.keys() else None,
            acus=row["acus"] or 0.0,
            acu_warned=row["acu_warned"] or 0,
            last_activity_at=row["last_activity_at"] or 0,
            quiet_alerted=bool(row["quiet_alerted"]),
            created_at=row["created_at"] or 0,
        )

    async def upsert_binding(self, b: Binding) -> None:
        if not b.created_at:
            b.created_at = int(time.time())
        await self._conn.execute(
            """INSERT INTO bindings
               (session_id, thread_id, channel_id, anchor_msg_id, title, url,
                status, status_detail, msg_cursor, seen_event_ids, active, model,
                last_msg, acus, acu_warned, last_activity_at, quiet_alerted,
                created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                 thread_id=excluded.thread_id, channel_id=excluded.channel_id,
                 anchor_msg_id=excluded.anchor_msg_id, title=excluded.title,
                 url=excluded.url, status=excluded.status,
                 status_detail=excluded.status_detail, msg_cursor=excluded.msg_cursor,
                 seen_event_ids=excluded.seen_event_ids, active=excluded.active,
                 model=excluded.model, last_msg=excluded.last_msg,
                 acus=excluded.acus, acu_warned=excluded.acu_warned,
                 last_activity_at=excluded.last_activity_at,
                 quiet_alerted=excluded.quiet_alerted""",
            (
                b.session_id, b.thread_id, b.channel_id, b.anchor_msg_id, b.title, b.url,
                b.status, b.status_detail, b.msg_cursor,
                # insertion order — evict the OLDEST ids, not the
                # lexicographically smallest (uuid ids don't sort by time)
                json.dumps(b.seen_event_ids[-SEEN_CAP:]),
                int(b.active), b.model, b.last_msg, b.acus, b.acu_warned,
                b.last_activity_at or None, int(b.quiet_alerted), b.created_at,
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
            last_notified=row["last_notified"],
            auto_merge=bool(row["auto_merge"]) if row["auto_merge"] is not None else None,
            updated_at=row["updated_at"],
        )

    async def upsert_pr(self, pr: PrRow) -> None:
        if not pr.updated_at:
            pr.updated_at = int(time.time())
        await self._conn.execute(
            """INSERT INTO prs
               (session_id, pr_url, owner, repo, number, pr_title, state,
                checks_state, card_msg_id, last_notified, auto_merge, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
                 -- NULL means "this upsert doesn't carry the flag" so a
                 -- state-only write can't silently flip the toggle off
                 auto_merge=COALESCE(excluded.auto_merge, prs.auto_merge),
                 updated_at=excluded.updated_at""",
            (
                pr.session_id, pr.pr_url, pr.owner, pr.repo, pr.number,
                pr.pr_title, pr.state, pr.checks_state, pr.card_msg_id,
                pr.last_notified,
                None if pr.auto_merge is None else int(pr.auto_merge),
                pr.updated_at,
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

    async def get_pr_by_ref(
        self, session_id: str, owner: str, repo: str, number: int
    ) -> PrRow | None:
        """Find a tracked PR by identity — case-insensitive owner/repo so a
        differently-cased URL or webhook payload still matches."""
        async with self._conn.execute(
            """SELECT * FROM prs WHERE session_id = ?
                 AND lower(owner) = lower(?) AND lower(repo) = lower(?)
                 AND number = ?""",
            (session_id, owner, repo, number),
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_pr(row) if row else None

    async def get_pr_by_card(
        self, session_id: str, card_msg_id: int
    ) -> PrRow | None:
        """Reverse lookup for emoji reactions: card message id → PrRow."""
        async with self._conn.execute(
            "SELECT * FROM prs WHERE session_id = ? AND card_msg_id = ?",
            (session_id, card_msg_id),
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
               WHERE lower(p.owner) = lower(?) AND lower(p.repo) = lower(?)
                 AND p.number = ?""",
            (owner, repo, number),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        # b.* and p.* share only the join key (session_id) — Row returns the
        # first column of a duplicated name, which is the same value anyway.
        return self._row_to_binding(row), self._row_to_pr(row)

    # ---- schedules --------------------------------------------------------

    @staticmethod
    def _row_to_schedule(row: sqlite3.Row) -> ScheduleRow:
        return ScheduleRow(
            id=row["id"], prompt=row["prompt"],
            repos=list(json.loads(row["repos"])), model=row["model"],
            interval_seconds=row["interval_seconds"],
            next_run_at=row["next_run_at"], enabled=bool(row["enabled"]),
            last_session_id=row["last_session_id"], created_at=row["created_at"],
        )

    async def add_schedule(self, s: ScheduleRow) -> int:
        """Insert a schedule row; returns its id."""
        if not s.created_at:
            s.created_at = int(time.time())
        cur = await self._conn.execute(
            """INSERT INTO schedules
               (prompt, repos, model, interval_seconds, next_run_at, enabled,
                last_session_id, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (s.prompt, json.dumps(s.repos), s.model, s.interval_seconds,
             s.next_run_at, int(s.enabled), s.last_session_id, s.created_at),
        )
        await self._conn.commit()
        return cur.lastrowid or 0

    async def due_schedules(self, now: int) -> list[ScheduleRow]:
        async with self._conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at <= ?",
            (now,),
        ) as cur:
            return [self._row_to_schedule(r) for r in await cur.fetchall()]

    async def all_schedules(self) -> list[ScheduleRow]:
        async with self._conn.execute(
            "SELECT * FROM schedules ORDER BY next_run_at"
        ) as cur:
            return [self._row_to_schedule(r) for r in await cur.fetchall()]

    async def schedule_ran(self, schedule_id: int, session_id: str, now: int) -> None:
        """Mark a fire and push next_run_at forward from NOW (not
        now-due + interval) so a long downtime can't fire a catch-up storm."""
        await self._conn.execute(
            """UPDATE schedules SET
                 next_run_at = ? + interval_seconds,
                 last_session_id = ?
               WHERE id = ?""",
            (now, session_id, schedule_id),
        )
        await self._conn.commit()

    async def delete_schedule(self, schedule_id: int) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM schedules WHERE id = ?", (schedule_id,)
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

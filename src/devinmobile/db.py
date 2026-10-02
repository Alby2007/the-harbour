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
    repos          TEXT,
    continued_from TEXT,
    max_acu        REAL,
    review_of      TEXT,
    chain          TEXT,
    summary        TEXT,
    spawned_by     TEXT,
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
    kind             TEXT NOT NULL DEFAULT 'spawn',  -- spawn | digest | monitor | inbox
    watch            TEXT,          -- monitor: https://… or ci:owner/repo[@branch]
    expect           TEXT,          -- monitor: required substring in URL body
    watch_state      TEXT NOT NULL DEFAULT '',  -- '' | green | red (last observed)
    last_fired_at    INTEGER,       -- monitor: last spawn (cooldown math)
    cooldown_seconds INTEGER NOT NULL DEFAULT 14400,  -- still-red re-fire floor
    spawned_by       TEXT,          -- creator's discord id — spawns inherit it
    created_at       INTEGER NOT NULL
);

-- Standing per-repo guidance ("tests are flaky — pytest -x"): injected into
-- every spawn's prompt, and written back by completions harvesting
-- structured_output.repo_notes so each session teaches the next.
CREATE TABLE IF NOT EXISTS repo_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT NOT NULL,   -- owner/repo as written; matched case-insensitively
    note        TEXT NOT NULL,
    created_by  TEXT,            -- discord user id or "devin:<session>" for harvested
    created_at  INTEGER NOT NULL,
    UNIQUE(repo, note)           -- dedupe: harvest re-runs can't double-save
);

-- Runtime-allowlisted operators (/allow, /deny). Unions with
-- ALLOWED_USER_IDS + REQUIRED_ROLE_ID at the gate — removing a row only
-- undoes /allow, never env-listed or role-based access.
CREATE TABLE IF NOT EXISTS allowed_users (
    discord_id  TEXT PRIMARY KEY,
    added_by    TEXT,
    created_at  INTEGER NOT NULL
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
        # comma-joined canonical owner/repo list — powers /usage per-repo
        # rollup; backfilled from prs where a session produced one
        "repos": "ALTER TABLE bindings ADD COLUMN repos TEXT",
        # session id this one continues from — set at spawn; a non-empty
        # value also caps auto-respawn chains at depth 1
        "continued_from": "ALTER TABLE bindings ADD COLUMN continued_from TEXT",
        # per-task ACU cap from /devin budget: (NULL = follow global)
        "max_acu": "ALTER TABLE bindings ADD COLUMN max_acu REAL",
        # "owner/repo#n" this session was spawned to review — marks it for
        # the Post-to-GitHub button on completion + dedupes label events
        "review_of": "ALTER TABLE bindings ADD COLUMN review_of TEXT",
        # playbook chain state (JSON) — travels on each phase's own row;
        # see chains.py for the shape
        "chain": "ALTER TABLE bindings ADD COLUMN chain TEXT",
        # structured_output.summary captured at completion — feeds /digest
        # and digest-kind schedules without N API calls
        "summary": "ALTER TABLE bindings ADD COLUMN summary TEXT",
        # who spawned it — a discord snowflake, or a marker like
        # 'github'/'intake'/a token-map name; '' = legacy/unattributed
        "spawned_by": "ALTER TABLE bindings ADD COLUMN spawned_by TEXT",
    },
    "prs": {
        "auto_merge": (
            "ALTER TABLE prs ADD COLUMN auto_merge INTEGER"
        ),
    },
    "schedules": {
        # 'spawn' (default) fires spawn_session; 'digest' posts a rollup;
        # 'monitor' checks first, spawns on a red edge; 'inbox' posts the
        # triage card
        "kind": (
            "ALTER TABLE schedules ADD COLUMN kind TEXT NOT NULL DEFAULT 'spawn'"
        ),
        "watch": "ALTER TABLE schedules ADD COLUMN watch TEXT",
        "expect": "ALTER TABLE schedules ADD COLUMN expect TEXT",
        "watch_state": (
            "ALTER TABLE schedules ADD COLUMN "
            "watch_state TEXT NOT NULL DEFAULT ''"
        ),
        "last_fired_at": (
            "ALTER TABLE schedules ADD COLUMN last_fired_at INTEGER"
        ),
        "cooldown_seconds": (
            "ALTER TABLE schedules ADD COLUMN "
            "cooldown_seconds INTEGER NOT NULL DEFAULT 14400"
        ),
        # the creator's discord id — every spawn the row fires inherits it
        "spawned_by": "ALTER TABLE schedules ADD COLUMN spawned_by TEXT",
    },
}

# How many event ids to keep for replay dedupe.
SEEN_CAP = 500


def _parse_chain(raw: str | None) -> dict | None:
    """bindings.chain JSON → dict; malformed/missing → None (no chain)."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


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
    kind: str = "spawn"  # spawn | digest | monitor | inbox
    watch: str = ""  # monitor: https://… URL or ci:owner/repo[@branch]
    expect: str = ""  # monitor: substring the URL body must contain
    watch_state: str = ""  # '' | green | red — last observed, for edges
    last_fired_at: int | None = None  # monitor: cooldown anchor
    cooldown_seconds: int = 14400  # still-red re-fire floor (4h)
    spawned_by: str = ""  # creator's discord id — spawns inherit it
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
    repos: str = ""  # comma-joined canonical owner/repo, set at spawn
    continued_from: str = ""  # parent session id for respawned/continued
    max_acu: float | None = None  # per-task cap; None = follow global
    review_of: str = ""  # "owner/repo#n" when spawned by the review label
    chain: dict | None = None  # playbook state — see chains.py for shape
    summary: str = ""  # structured_output.summary captured at completion
    # who spawned it — discord snowflake, or a marker ('github', 'intake',
    # a token-map name); '' = legacy/unattributed. mention_for() routes
    # pings through this: snowflake → owner ping, else all-allowlist.
    spawned_by: str = ""
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
        # Backfill: sessions that produced a PR get their repo from the prs
        # table; PR-less history stays repo-less and groups under "(no repo)".
        await conn.execute(
            """UPDATE bindings SET repos = (
                 SELECT p.owner || '/' || p.repo FROM prs p
                   WHERE p.session_id = bindings.session_id
                     AND p.owner != '' AND p.repo != '' LIMIT 1)
               WHERE (repos IS NULL OR repos = '')
                 AND EXISTS (SELECT 1 FROM prs p2
                    WHERE p2.session_id = bindings.session_id
                      AND p2.owner != '' AND p2.repo != '')"""
        )
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    # ---- Runtime allowlist (env ∪ role ∪ this table, at the gate) --------

    async def is_allowed_user(self, discord_id) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM allowed_users WHERE discord_id = ?",
            (str(discord_id),),
        ) as cur:
            return await cur.fetchone() is not None

    async def add_allowed_user(self, discord_id, added_by=None) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO allowed_users"
            " (discord_id, added_by, created_at) VALUES (?,?,?)",
            (str(discord_id), None if added_by is None else str(added_by),
             int(time.time())),
        )
        await self._conn.commit()

    async def remove_allowed_user(self, discord_id) -> bool:
        """True when a row existed — env-listed ids are never in here."""
        cur = await self._conn.execute(
            "DELETE FROM allowed_users WHERE discord_id = ?",
            (str(discord_id),),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def acu_by_user(self, spawned_by: str, since: int) -> float:
        """24h ACU spend for a user — session-start attribution (the
        /usage caveat: spawned_by stamps at spawn, not over the run)."""
        async with self._conn.execute(
            "SELECT COALESCE(SUM(acus), 0) FROM bindings"
            " WHERE spawned_by = ? AND created_at >= ?",
            (spawned_by, since),
        ) as cur:
            row = await cur.fetchone()
        return float(row[0]) if row else 0.0

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
            repos=(row["repos"] or "") if "repos" in row.keys() else "",
            continued_from=(
                (row["continued_from"] or "")
                if "continued_from" in row.keys() else ""
            ),
            max_acu=row["max_acu"] if "max_acu" in row.keys() else None,
            review_of=(
                (row["review_of"] or "") if "review_of" in row.keys() else ""
            ),
            chain=(
                _parse_chain(row["chain"])
                if "chain" in row.keys() else None
            ),
            summary=(row["summary"] or "") if "summary" in row.keys() else "",
            spawned_by=(
                (row["spawned_by"] or "")
                if "spawned_by" in row.keys() else ""
            ),
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
                repos, continued_from, max_acu, review_of, chain, summary,
                spawned_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                 thread_id=excluded.thread_id, channel_id=excluded.channel_id,
                 anchor_msg_id=excluded.anchor_msg_id, title=excluded.title,
                 url=excluded.url, status=excluded.status,
                 status_detail=excluded.status_detail, msg_cursor=excluded.msg_cursor,
                 seen_event_ids=excluded.seen_event_ids, active=excluded.active,
                 model=excluded.model, last_msg=excluded.last_msg,
                 acus=excluded.acus, acu_warned=excluded.acu_warned,
                 last_activity_at=excluded.last_activity_at,
                 quiet_alerted=excluded.quiet_alerted,
                 -- a state-only upsert (repos="") must not wipe the
                 -- spawn-time repo attribution
                 repos=COALESCE(NULLIF(excluded.repos, ''), bindings.repos),
                 continued_from=COALESCE(NULLIF(excluded.continued_from, ''),
                                         bindings.continued_from),
                 max_acu=COALESCE(excluded.max_acu, bindings.max_acu),
                 review_of=COALESCE(NULLIF(excluded.review_of, ''),
                                    bindings.review_of),
                 chain=COALESCE(NULLIF(excluded.chain, ''),
                                bindings.chain),
                 -- same convention: a state-only upsert must not wipe the
                 -- completion-captured summary
                 summary=COALESCE(NULLIF(excluded.summary, ''),
                                  bindings.summary),
                 -- or the spawn-time owner attribution
                 spawned_by=COALESCE(NULLIF(excluded.spawned_by, ''),
                                     bindings.spawned_by)""",
            (
                b.session_id, b.thread_id, b.channel_id, b.anchor_msg_id, b.title, b.url,
                b.status, b.status_detail, b.msg_cursor,
                # insertion order — evict the OLDEST ids, not the
                # lexicographically smallest (uuid ids don't sort by time)
                json.dumps(b.seen_event_ids[-SEEN_CAP:]),
                int(b.active), b.model, b.last_msg, b.acus, b.acu_warned,
                b.last_activity_at or None, int(b.quiet_alerted),
                b.repos or None, b.continued_from or None, b.max_acu,
                b.review_of or None,
                json.dumps(b.chain) if b.chain else None,
                b.summary or None, b.spawned_by or None, b.created_at,
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

    async def bindings_since(self, since: int) -> list[Binding]:
        """Digest window — a session counts if it was active in the window
        (last_activity_at falls back to created_at) OR is still polling —
        a long silent turn bumps no timestamps but belongs in the rollup."""
        async with self._conn.execute(
            """SELECT * FROM bindings
               WHERE COALESCE(last_activity_at, created_at) >= ?
                  OR active = 1
               ORDER BY created_at""",
            (since,),
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

    async def binding_by_review_of(self, review_of: str) -> Binding | None:
        """Dedupe review-label spawns — one review session per PR."""
        async with self._conn.execute(
            "SELECT * FROM bindings WHERE review_of = ? LIMIT 1", (review_of,)
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_binding(row) if row else None

    async def child_of(self, session_id: str) -> Binding | None:
        """The continuation child of a session — the idempotency key for
        chain advance across a crash between 'decided' and 'spawned'."""
        async with self._conn.execute(
            "SELECT * FROM bindings WHERE continued_from = ? LIMIT 1",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_binding(row) if row else None

    async def chain_resumable(self) -> list[Binding]:
        """Completed chain-phase bindings — the resume sweep filters
        terminal-marked/pending/already-continued rows Python-side."""
        async with self._conn.execute(
            "SELECT * FROM bindings WHERE chain IS NOT NULL AND chain != ''"
            " AND status = 'exit'"
        ) as cur:
            return [self._row_to_binding(r) for r in await cur.fetchall()]

    async def chained_bindings(self, limit: int = 25) -> list[Binding]:
        """All bindings carrying chain state — backs /chains."""
        async with self._conn.execute(
            "SELECT * FROM bindings WHERE chain IS NOT NULL AND chain != ''"
            " ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            return [self._row_to_binding(r) for r in await cur.fetchall()]

    async def has_armed_open_pr(self, session_id: str) -> bool:
        """An auto_merge-armed PR still open — keeps a dead session's
        binding polling so the merge can actually fire when CI greens."""
        async with self._conn.execute(
            "SELECT 1 FROM prs WHERE session_id = ? AND auto_merge = 1"
            " AND state = 'open' LIMIT 1",
            (session_id,),
        ) as cur:
            return await cur.fetchone() is not None

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
            last_session_id=row["last_session_id"],
            kind=row["kind"] or "spawn",
            watch=row["watch"] or "",
            expect=row["expect"] or "",
            watch_state=row["watch_state"] or "",
            last_fired_at=row["last_fired_at"],
            cooldown_seconds=row["cooldown_seconds"] or 14400,
            spawned_by=row["spawned_by"] or "",
            created_at=row["created_at"],
        )

    async def add_schedule(self, s: ScheduleRow) -> int:
        """Insert a schedule row; returns its id."""
        if not s.created_at:
            s.created_at = int(time.time())
        cur = await self._conn.execute(
            """INSERT INTO schedules
               (prompt, repos, model, interval_seconds, next_run_at, enabled,
                last_session_id, kind, watch, expect, watch_state,
                last_fired_at, cooldown_seconds, spawned_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (s.prompt, json.dumps(s.repos), s.model, s.interval_seconds,
             s.next_run_at, int(s.enabled), s.last_session_id, s.kind,
             s.watch or None, s.expect or None, s.watch_state,
             s.last_fired_at, s.cooldown_seconds, s.spawned_by or None,
             s.created_at),
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

    async def get_schedule(self, schedule_id: int) -> ScheduleRow | None:
        """One row — the /unschedule owner-check needs spawned_by before
        deciding whether the caller may delete it."""
        async with self._conn.execute(
            "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_schedule(row) if row else None

    async def delete_schedule(self, schedule_id: int) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM schedules WHERE id = ?", (schedule_id,)
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def update_watch(
        self, schedule_id: int, state: str, last_fired_at: int | None = None
    ) -> None:
        """Monitor bookkeeping — watch_state edge detection + the cooldown
        anchor. last_fired_at=None keeps the previous value."""
        await self._conn.execute(
            """UPDATE schedules SET
                 watch_state = ?,
                 last_fired_at = COALESCE(?, last_fired_at)
               WHERE id = ?""",
            (state, last_fired_at, schedule_id),
        )
        await self._conn.commit()

    # ---- inbox ------------------------------------------------------------

    async def inbox_bindings(self) -> list[Binding]:
        """Rows the triage card might care about: still-polling, errored, or
        carrying chain state (pending-Continue rows). Python-side filters
        do the sectioning — the query is deliberately broad."""
        async with self._conn.execute(
            """SELECT * FROM bindings
               WHERE active = 1 OR status = 'error'
                  OR (chain IS NOT NULL AND chain != '')
               ORDER BY created_at DESC"""
        ) as cur:
            return [self._row_to_binding(r) for r in await cur.fetchall()]

    async def continued_parents(self) -> set[str]:
        """Session ids that already produced a continuation child — an
        errored session with a respawn/`/continue` child isn't inbox work."""
        async with self._conn.execute(
            "SELECT DISTINCT continued_from FROM bindings"
            " WHERE continued_from IS NOT NULL AND continued_from != ''"
        ) as cur:
            return {r[0] for r in await cur.fetchall()}

    async def open_prs(self) -> list[PrRow]:
        async with self._conn.execute(
            "SELECT * FROM prs WHERE state = 'open' ORDER BY updated_at DESC"
        ) as cur:
            return [self._row_to_pr(r) for r in await cur.fetchall()]

    async def idle_repos(self, since: int, limit: int = 5) -> list[str]:
        """Repos we know (notes or PR history) with NO binding activity in
        the window — the inbox-zero 'habit nudge' suggestions."""
        async with self._conn.execute(
            "SELECT DISTINCT repo FROM repo_notes"
        ) as cur:
            known = {r[0] for r in await cur.fetchall()}
        async with self._conn.execute(
            "SELECT DISTINCT owner || '/' || repo FROM prs"
            " WHERE owner != '' AND repo != ''"
        ) as cur:
            known |= {r[0] for r in await cur.fetchall()}
        async with self._conn.execute(
            """SELECT repos FROM bindings
               WHERE COALESCE(last_activity_at, created_at) >= ?""",
            (since,),
        ) as cur:
            busy = {
                repo.strip().lower()
                for (csv,) in await cur.fetchall()
                for repo in (csv or "").split(",")
                if repo.strip()
            }
        # subtract case-insensitively but keep `known`'s written case —
        # repo_notes casing and canonical binding repos can disagree
        return sorted(r for r in known if r.lower() not in busy)[:limit]

    # ---- repo notes ---------------------------------------------------------

    async def add_note(
        self, repo: str, note: str, created_by: str | None = None
    ) -> bool:
        """Insert a standing repo note; False when (repo, note) exists —
        auto-harvest re-runs can't double-save."""
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO repo_notes"
            " (repo, note, created_by, created_at) VALUES (?,?,?,?)",
            (repo, note, created_by, int(time.time())),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def notes_for_repos(self, repos: list[str]) -> dict[str, list[str]]:
        """repo -> newest-first note texts (≤8/repo) for prompt injection;
        repo match is case-insensitive."""
        if not repos:
            return {}
        marks = ",".join("?" for _ in repos)
        async with self._conn.execute(
            "SELECT repo, note FROM repo_notes"
            f" WHERE LOWER(repo) IN ({marks})"
            " ORDER BY created_at DESC, id DESC",
            tuple(r.lower() for r in repos),
        ) as cur:
            out: dict[str, list[str]] = {}
            for row in await cur.fetchall():
                bucket = out.setdefault(row["repo"], [])
                if len(bucket) < 8:
                    bucket.append(row["note"])
            return out

    async def list_notes(
        self, repo: str | None = None
    ) -> list[tuple[int, str, str]]:
        """(id, repo, note) newest-first, ≤20 rows — backs /notes."""
        if repo:
            sql = (
                "SELECT id, repo, note FROM repo_notes WHERE LOWER(repo) = ?"
                " ORDER BY created_at DESC, id DESC LIMIT 20"
            )
            params: tuple = (repo.lower(),)
        else:
            sql = (
                "SELECT id, repo, note FROM repo_notes"
                " ORDER BY created_at DESC, id DESC LIMIT 20"
            )
            params = ()
        async with self._conn.execute(sql, params) as cur:
            return [(r["id"], r["repo"], r["note"]) for r in await cur.fetchall()]

    async def note_created_by(self, note_id: int) -> str | None:
        """The note's owner for the /unnote admin gate; None = no row."""
        async with self._conn.execute(
            "SELECT created_by FROM repo_notes WHERE id = ?", (note_id,)
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else (row["created_by"] or "")

    async def delete_note(self, note_id: int) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM repo_notes WHERE id = ?", (note_id,)
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

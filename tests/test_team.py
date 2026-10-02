"""Small-team layer — role gating (REQUIRED_ROLE_ID), GitHub-sender
attribution (GITHUB_USER_MAP), per-user ACU quotas (USER_ACU_DAILY),
admin-gated destructive ops (TEAM_ADMIN_IDS), hub lanes
(HUB_CHANNEL_MAP), the runtime allowlist (/allow /deny), and
/inbox mine:. Every phase is off until its env var is set."""

import time
from types import SimpleNamespace

import pytest

import devinmobile.spawn as spawn_mod
from devinmobile.bot.commands import _is_operator, register_commands, user_role_ids
from devinmobile.config import Settings
from devinmobile.db import Binding, Database, PrRow, ScheduleRow
from devinmobile.inbox import build_inbox_embed
from devinmobile.models import Session
from devinmobile.spawn import SpawnError
from devinmobile.webhook_server import WebhookServer


async def _db(tmp_path) -> Database:
    return await Database.connect(str(tmp_path / "t.db"))


def _settings(**kw) -> Settings:
    # _env_file=None keeps the repo's real .env out of tests
    return Settings(_env_file=None, allowed_user_ids="1", **kw)


# ---- Phase A: role-based operator access --------------------------------------


def test_is_operator_truth_table():
    s = _settings(required_role_id=55)
    assert s.is_operator(1, [])            # allowlisted id
    assert s.is_operator(2, [55, 77])      # carries the role
    assert s.is_operator(1, [55])          # both
    assert not s.is_operator(2, [])        # neither
    assert not s.is_operator(2, [77])      # wrong role only
    # unset → allowlist only (the solo default is preserved)
    assert _settings().is_operator(1, [])
    assert not _settings().is_operator(2, [55])


def test_dm_context_is_id_only():
    """A DM `User` object has no .roles — user_role_ids extracts nothing,
    so REQUIRED_ROLE_ID can't grant DM access."""
    s = _settings(required_role_id=55)
    dm_user = type("U", (), {"id": 2})()          # no roles attr
    member = type("M", (), {
        "id": 2,
        "roles": [type("R", (), {"id": 55})()],
    })()
    assert user_role_ids(dm_user) == []
    assert not s.is_operator(dm_user.id, user_role_ids(dm_user))
    assert user_role_ids(member) == [55]
    assert s.is_operator(member.id, user_role_ids(member))


async def test_is_operator_unions_the_db_allowlist(tmp_path):
    db = await _db(tmp_path)
    bot = SimpleNamespace(settings=_settings(), db=db)
    assert not await _is_operator(bot, 2)
    await db.add_allowed_user(2, added_by=1)
    assert await _is_operator(bot, 2)
    await db.remove_allowed_user(2)
    assert not await _is_operator(bot, 2)


# ---- Phase B: github sender attribution ----------------------------------------


class _FakeThread:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str, **kw) -> None:
        self.sent.append(text)


class _FakeBot:
    def __init__(self, thread, settings) -> None:
        self._thread = thread
        self.settings = settings

    def get_channel(self, _id):
        return self._thread


def _issue_payload(sender_login: str) -> dict:
    return {
        "action": "labeled",
        "label": {"name": "devin"},
        "issue": {"number": 7, "title": "Fix crash",
                  "body": "it dies", "html_url": "u"},
        "repository": {"name": "r", "owner": {"login": "o"}},
        "sender": {"login": sender_login},
    }


async def _label_spawn(tmp_path, monkeypatch, settings, payload):
    spawned: list[dict] = []

    async def fake_spawn(bot, **kw):
        spawned.append(kw)
        return SimpleNamespace(session_id="s9"), _FakeThread()

    monkeypatch.setattr(spawn_mod, "spawn_session", fake_spawn)
    db = await _db(tmp_path)
    srv = WebhookServer(_FakeBot(_FakeThread(), settings), db, settings)
    await srv._on_issue(payload)
    return spawned


async def test_github_sender_mapped_gets_own_attribution(tmp_path, monkeypatch):
    s = _settings(github_user_map="octocat:42")
    spawned = await _label_spawn(
        tmp_path, monkeypatch, s, _issue_payload("octocat")
    )
    assert spawned[0]["spawned_by"] == "42"


async def test_github_sender_unmapped_keeps_marker(tmp_path, monkeypatch):
    s = _settings(github_user_map="octocat:42")
    spawned = await _label_spawn(
        tmp_path, monkeypatch, s, _issue_payload("stranger")
    )
    assert spawned[0]["spawned_by"] == "github"


# ---- spawn-path harness (quota + lanes) ----------------------------------------


class _Chan:
    id = 4242

    def __init__(self):
        self.sent: list = []

    async def send(self, *a, **kw):
        self.sent.append(a[0] if a else "")
        return SimpleNamespace(id=900)

    async def join(self):
        pass


class _Hub:
    """Stands in for a discord.TextChannel (monkeypatched isinstance)."""

    def __init__(self, cid):
        self.id = cid
        self.threads: list[_Chan] = []

    async def create_thread(self, name, type=None):
        t = _Chan()
        self.threads.append(t)
        return t


class _Devin:
    def __init__(self):
        self.created: list[dict] = []

    async def create_session(self, **kw):
        self.created.append(kw)
        return Session(session_id="s9", url="u9", status="running")


class _RelayStub:
    def __init__(self):
        self.progress = SimpleNamespace(thinking=self._thinking)

    async def _thinking(self, binding):
        pass

    def request_poll(self, b):
        pass


class _SpawnSettings:
    allowed_user_id_set = {1, 2}
    hub_channel_id = 42
    hub_channel_id_map: dict = {}
    user_acu_daily = 0.0
    devin_user_id_map: dict = {}
    create_as_user_id = None
    default_model = None
    devin_mode = "normal"
    max_acu_limit = 25

    def is_operator(self, user_id, role_ids=()):
        return user_id in self.allowed_user_id_set


class _SpawnBot:
    """spawn_session's full surface — records the channel id asked for."""

    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.devin = _Devin()
        self.relay = _RelayStub()
        self.bridge = type("B", (), {"available": False})()
        self.channels: dict[int, _Hub] = {}
        self.requested: list[int] = []
        self.user = object()

    def get_channel(self, cid):
        self.requested.append(cid)
        return self.channels.setdefault(cid, _Hub(cid))

    async def fetch_channel(self, cid):
        self.requested.append(cid)
        return self.channels.setdefault(cid, _Hub(cid))

    async def handle_component(self, *a):
        pass


# ---- Phase C: per-user ACU quota -------------------------------------------------


async def test_acu_by_user_window(tmp_path):
    db = await _db(tmp_path)
    now = int(time.time())
    for sid, who, acus, age in [
        ("a", "7", 2.0, now - 100), ("b", "7", 3.0, now - 500),
        ("old", "7", 9.0, now - 90000),  # outside the 24h window
        ("other", "8", 4.0, now - 100),
    ]:
        await db.upsert_binding(Binding(
            session_id=sid, thread_id=1, channel_id=1,
            spawned_by=who, acus=acus, created_at=age,
        ))
    assert await db.acu_by_user("7", now - 86400) == 5.0
    assert await db.acu_by_user("8", now - 86400) == 4.0
    assert await db.acu_by_user("nobody", now - 86400) == 0.0


async def test_quota_blocks_at_limit(tmp_path, monkeypatch):
    import discord

    db = await _db(tmp_path)
    await db.upsert_binding(Binding(
        session_id="spent", thread_id=1, channel_id=1,
        spawned_by="1", acus=5.0,
    ))
    s = _SpawnSettings()
    s.user_acu_daily = 5.0
    bot = _SpawnBot(db, s)
    monkeypatch.setattr(discord, "TextChannel", _Hub)
    with pytest.raises(SpawnError, match="daily ACU quota 5 reached"):
        await spawn_mod.spawn_session(bot, prompt="x", spawned_by="1")
    assert not bot.devin.created  # never reached the API


async def test_quota_allows_below_and_exempts_markers(tmp_path, monkeypatch):
    import discord

    db = await _db(tmp_path)
    await db.upsert_binding(Binding(
        session_id="spent", thread_id=1, channel_id=1,
        spawned_by="1", acus=4.9,
    ))
    s = _SpawnSettings()
    s.user_acu_daily = 5.0
    bot = _SpawnBot(db, s)
    monkeypatch.setattr(discord, "TextChannel", _Hub)
    # 4.9 < 5 → allowed
    await spawn_mod.spawn_session(bot, prompt="x", spawned_by="1")
    # marker spawned_by is exempt even if quota is configured — team
    # infra (label spawns, /task intake) isn't a user
    await db.upsert_binding(Binding(
        session_id="gh1", thread_id=2, channel_id=1,
        spawned_by="github", acus=50.0,
    ))
    await spawn_mod.spawn_session(bot, prompt="x", spawned_by="github")
    assert len(bot.devin.created) == 2


async def test_quota_off_by_default(tmp_path, monkeypatch):
    import discord

    db = await _db(tmp_path)
    await db.upsert_binding(Binding(
        session_id="spent", thread_id=1, channel_id=1,
        spawned_by="1", acus=500.0,
    ))
    bot = _SpawnBot(db, _SpawnSettings())  # user_acu_daily = 0
    monkeypatch.setattr(discord, "TextChannel", _Hub)
    await spawn_mod.spawn_session(bot, prompt="x", spawned_by="1")
    assert len(bot.devin.created) == 1


# ---- Phase E: per-user hub lanes --------------------------------------------------


async def test_mapped_user_spawns_into_their_lane(tmp_path, monkeypatch):
    import discord

    db = await _db(tmp_path)
    s = _SpawnSettings()
    s.hub_channel_id_map = {2: 777}
    bot = _SpawnBot(db, s)
    monkeypatch.setattr(discord, "TextChannel", _Hub)
    await spawn_mod.spawn_session(bot, prompt="x", spawned_by="2")
    assert bot.requested == [777]
    b = await db.get_binding("s9")
    assert b is not None and b.channel_id == 777


async def test_markers_and_unlisted_ids_use_default_hub(tmp_path, monkeypatch):
    import discord

    db = await _db(tmp_path)
    s = _SpawnSettings()
    s.hub_channel_id_map = {2: 777, 999: 888}
    bot = _SpawnBot(db, s)
    monkeypatch.setattr(discord, "TextChannel", _Hub)
    # markers never lane
    await spawn_mod.spawn_session(bot, prompt="x", spawned_by="github")
    # an allowlisted user with no lane → default
    await spawn_mod.spawn_session(bot, prompt="x", spawned_by="1")
    # a digit that ISN'T allowlisted can't be redirected into a lane —
    # same spoof guard as devin_user_map (caller-supplied /task `by:`)
    await spawn_mod.spawn_session(bot, prompt="x", spawned_by="999")
    assert bot.requested == [42, 42, 42]


# ---- Phase D/F command harness ----------------------------------------------------


class _Tree:
    def __init__(self):
        self.commands: dict = {}

    def command(self, *a, **kw):
        def deco(fn):
            self.commands[kw.get("name")] = fn
            return fn
        return deco


class _Resp:
    def __init__(self):
        self.sent: list[str] = []
        self.deferred = False

    async def send_message(self, text="", **kw):
        self.sent.append(text if isinstance(text, str) else "")

    async def send(self, text="", **kw):  # followup.send has same shape
        await self.send_message(text, **kw)

    async def defer(self, **kw):
        self.deferred = True

    def is_done(self):
        return bool(self.sent) or self.deferred


class _Ix:
    def __init__(self, uid=1, channel=None, fetch_channel=None):
        self.user = type("U", (), {"id": uid})()
        self.response = _Resp()
        self.followup = _Resp()
        self.channel = channel

        async def _fetch(cid):
            return None

        self.client = SimpleNamespace(
            fetch_channel=fetch_channel or _fetch
        )


def _cmd_bot(db, admins=frozenset({9}), allowed=None):
    bot = SimpleNamespace(tree=_Tree(), db=db)
    bot.settings = type(
        "S", (), {
            "allowed_user_id_set": allowed if allowed is not None else {1, 2, 9},
            "admin_user_id_set": admins,
            "github_enabled": False,
            "is_operator": (
                lambda self, uid, role_ids=():
                uid in self.allowed_user_id_set
            ),
            "is_admin": (
                lambda self, uid: uid in self.admin_user_id_set
            ),
        },
    )()
    register_commands(bot)
    return bot


async def _killable_binding(db, spawned_by):
    await db.upsert_binding(Binding(
        session_id="s1", thread_id=10, channel_id=5,
        spawned_by=spawned_by, status="running",
    ))


# ---- Phase D: admin-gated destructive ops -----------------------------------------


async def test_kill_denies_non_owner_without_admin(tmp_path):
    db = await _db(tmp_path)
    await _killable_binding(db, spawned_by="2")
    bot = _cmd_bot(db)
    bot.devin = SimpleNamespace(terminate_session=lambda s: None)
    ix = _Ix(uid=1)  # not the owner, not an admin
    await bot.tree.commands["kill"](ix, session="s1")
    assert "owner or admin" in ix.response.sent[0]
    assert (await db.get_binding("s1")).active  # untouched


async def _ran_kill(db, uid):
    """A kill that passes the gate — fake devin terminates, binding parks."""
    bot = _cmd_bot(db)
    terminated: list[str] = []

    async def _term(sid):
        terminated.append(sid)

    bot.devin = SimpleNamespace(terminate_session=_term)

    async def _fetch(cid):
        return SimpleNamespace()  # not a Thread → archive path skips

    ix = _Ix(uid=uid, fetch_channel=_fetch)
    await bot.tree.commands["kill"](ix, session="s1")
    return terminated, ix


async def test_kill_owner_and_admin_allowed(tmp_path):
    db = await _db(tmp_path)
    await _killable_binding(db, spawned_by="2")
    terminated, ix = await _ran_kill(db, uid=2)  # the owner
    assert terminated == ["s1"]
    assert not (await db.get_binding("s1")).active

    await _killable_binding(db, spawned_by="2")  # re-arm
    terminated, ix = await _ran_kill(db, uid=9)  # an admin
    assert terminated == ["s1"]


async def test_kill_marker_sessions_stay_shared(tmp_path):
    # "github"/"intake"/"" owners are team infra — never gated
    db = await _db(tmp_path)
    await _killable_binding(db, spawned_by="github")
    terminated, _ = await _ran_kill(db, uid=1)
    assert terminated == ["s1"]


async def test_kill_flat_trust_without_admins(tmp_path):
    db = await _db(tmp_path)
    await _killable_binding(db, spawned_by="2")
    bot = _cmd_bot(db, admins=frozenset())  # TEAM_ADMIN_IDS unset
    terminated: list[str] = []

    async def _term(sid):
        terminated.append(sid)

    bot.devin = SimpleNamespace(terminate_session=_term)

    async def _fetch(cid):
        return SimpleNamespace()

    ix = _Ix(uid=1, fetch_channel=_fetch)
    await bot.tree.commands["kill"](ix, session="s1")
    assert terminated == ["s1"]  # today's flat behavior preserved


async def _scheduled(db, spawned_by):
    row = ScheduleRow(
        id=0, prompt="p", interval_seconds=3600,
        next_run_at=int(time.time()) + 3600, spawned_by=spawned_by,
    )
    row.id = await db.add_schedule(row)
    return row.id


async def test_unschedule_owner_gate(tmp_path):
    db = await _db(tmp_path)
    sid = await _scheduled(db, spawned_by="2")
    bot = _cmd_bot(db)
    ix = _Ix(uid=1)
    await bot.tree.commands["unschedule"](ix, schedule_id=sid)
    assert "owner or admin" in ix.response.sent[0]
    assert await db.get_schedule(sid) is not None

    ix = _Ix(uid=9)  # admin
    await bot.tree.commands["unschedule"](ix, schedule_id=sid)
    assert "Deleted" in ix.response.sent[0]
    assert await db.get_schedule(sid) is None


async def test_unnote_owner_gate(tmp_path):
    db = await _db(tmp_path)
    await db.add_note("o/r", "pytest -x", created_by="2")
    nid = (await db.list_notes())[0][0]
    bot = _cmd_bot(db)
    ix = _Ix(uid=1)
    await bot.tree.commands["unnote"](ix, note_id=nid)
    assert "owner or admin" in ix.response.sent[0]

    ix = _Ix(uid=9)  # admin
    await bot.tree.commands["unnote"](ix, note_id=nid)
    assert "Deleted" in ix.response.sent[0]
    assert await db.list_notes() == []


# ---- Phase F: runtime allowlist ----------------------------------------------------


async def test_allow_deny_admin_cycle(tmp_path):
    db = await _db(tmp_path)
    bot = _cmd_bot(db, admins=frozenset({9}))
    grantee = type("U", (), {"id": 77, "mention": "<@77>"})()

    # non-admin refused — operators can't self-escalate via the bot
    ix = _Ix(uid=1)
    await bot.tree.commands["allow"](ix, user=grantee)
    assert "Admins only" in ix.response.sent[0]
    assert not await db.is_allowed_user(77)

    ix = _Ix(uid=9)  # admin
    await bot.tree.commands["allow"](ix, user=grantee)
    assert "Allowed <@77>" in ix.response.sent[0]
    assert await db.is_allowed_user(77)
    # …and the gate honors it even though 77 is in neither env list nor role
    assert await _is_operator(bot, 77)

    ix = _Ix(uid=9)
    await bot.tree.commands["deny"](ix, user=grantee)
    assert "Removed <@77>" in ix.response.sent[0]
    assert not await db.is_allowed_user(77)


async def test_allow_refuses_without_admins(tmp_path):
    db = await _db(tmp_path)
    bot = _cmd_bot(db, admins=frozenset())  # TEAM_ADMIN_IDS unset
    ix = _Ix(uid=9)
    await bot.tree.commands["allow"](
        ix, user=type("U", (), {"id": 77, "mention": "<@77>"})()
    )
    assert "No admins configured" in ix.response.sent[0]
    assert not await db.is_allowed_user(77)


# ---- Phase G: /inbox mine: ---------------------------------------------------------


async def test_inbox_mine_filters_owned_sections(tmp_path):
    db = await _db(tmp_path)
    await db.upsert_binding(Binding(
        session_id="mine", thread_id=1, channel_id=5, active=True,
        status="blocked", status_detail="waiting_for_user",
        title="My waiting", spawned_by="1",
    ))
    await db.upsert_binding(Binding(
        session_id="theirs", thread_id=2, channel_id=5, active=True,
        status="blocked", status_detail="waiting_for_user",
        title="Their waiting", spawned_by="2",
    ))
    await db.upsert_binding(Binding(
        session_id="my-err", thread_id=3, channel_id=5, active=False,
        status="error", title="My error", spawned_by="1",
    ))
    await db.upsert_binding(Binding(
        session_id="their-err", thread_id=4, channel_id=5, active=False,
        status="error", title="Their error", spawned_by="2",
    ))
    await db.upsert_binding(Binding(
        session_id="their-chain", thread_id=5, channel_id=5, active=False,
        status="exit", title="Their chain", spawned_by="2",
        chain={"pending": 1},
    ))
    # shared infra rows — never filtered
    sched = ScheduleRow(
        id=0, prompt="m", interval_seconds=3600, kind="monitor",
        watch="https://h/health", watch_state="red",
        next_run_at=int(time.time()) + 3600,
    )
    await db.add_schedule(sched)
    await db.upsert_pr(PrRow(
        session_id="s", pr_url="https://x/1", owner="o", repo="r",
        number=1, state="open",
    ))

    embed = await build_inbox_embed(db, owner="1")
    names = [f.name for f in embed.fields]
    body = "\n".join(f.value for f in embed.fields)
    assert "Waiting on you (1)" in names
    assert "Errored (1)" in names
    assert not any("Chains awaiting" in n for n in names)
    assert "My waiting" in body and "Their waiting" not in body
    assert "Their chain" not in body
    # monitors + PRs stay shared — team infra, not a personal queue
    assert "Monitors red (1)" in names
    assert "Open PRs (1)" in names

    embed = await build_inbox_embed(db)  # no filter — everyone
    body = "\n".join(f.value for f in embed.fields)
    assert "Their waiting" in body and "Their chain" in body

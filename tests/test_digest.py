"""Digest — bindings_since window query, build_digest grouping, the
completion-time summary write, and digest-kind schedule firing."""

import time

import discord

import devinmobile.scheduler as sched_mod
from devinmobile.db import Binding, Database, ScheduleRow
from devinmobile.digest import build_digest
from devinmobile.models import Session
from devinmobile.relay import Notification, Relay
from devinmobile.scheduler import Scheduler


class _Settings:
    allowed_user_id_set = {1}
    max_acu_limit = 25
    auto_respawn = True
    silence_alert_minutes = 0
    hub_channel_id = 42


async def _db(tmp_path) -> Database:
    return await Database.connect(str(tmp_path / "t.db"))


class _Chan(discord.abc.Messageable):
    """discord.abc.Messageable declares __slots__=() — redeclare to hold
    the send log. _fire_digest isinstance-checks the hub channel."""

    __slots__ = ("sent",)

    def __init__(self):
        self.sent: list = []

    def _get_channel(self):
        return self

    async def send(self, *a, **kw):
        self.sent.append((a, kw))
        return None


# ---- bindings_since -------------------------------------------------------


async def test_bindings_since_window(tmp_path):
    db = await _db(tmp_path)
    now = int(time.time())
    old = Binding(session_id="old", thread_id=1, channel_id=5,
                  created_at=now - 10_000, active=False)
    fresh = Binding(session_id="new", thread_id=2, channel_id=5,
                    created_at=now - 60, active=False)
    longrun = Binding(session_id="long", thread_id=3, channel_id=5,
                      created_at=now - 10_000, last_activity_at=now - 30,
                      active=False)
    # still polling but silent — no timestamps land inside the window,
    # active=1 is what keeps it visible to the digest
    stuck = Binding(session_id="stuck", thread_id=4, channel_id=5,
                    created_at=now - 10_000, last_activity_at=now - 9_000,
                    active=True)
    for b in (old, fresh, longrun, stuck):
        await db.upsert_binding(b)
    ids = {b.session_id for b in await db.bindings_since(now - 3600)}
    # old's last activity predates the window; long-runners count via
    # last_activity_at, fresh via created_at, stuck via active
    assert ids == {"new", "long", "stuck"}


# ---- build_digest ----------------------------------------------------------


def _b(sid, status, **kw) -> Binding:
    return Binding(session_id=sid, thread_id=1, channel_id=5,
                   status=status, **kw)


def test_build_digest_groups_and_totals():
    since = int(time.time()) - 86400
    embed = build_digest([
        _b("done", "exit", title="Fix A", summary="patched the flake",
           acus=2.5),
        _b("run", "running", title="WIP"),
        _b("bad", "error", title="Oops"),
        _b("parked", "suspended", title="Paused"),
    ], since)
    names = [f.name for f in embed.fields]
    assert names == ["Completed", "Errored", "Suspended", "In flight"]
    done = embed.fields[0].value
    assert "Fix A" in done and "patched the flake" in done and "<#1>" in done
    assert "2.5 ACU" in done
    assert "4 sessions · 2.5 ACU" in (embed.footer.text or "")
    # <t:> renders in descriptions/fields but NOT in embed titles
    assert embed.title == "Devin digest"
    assert "<t:" in (embed.description or "")


def test_build_digest_empty_and_truncation():
    embed = build_digest([], int(time.time()))
    assert "Quiet" in (embed.description or "")
    long_sum = "x" * 500
    embed = build_digest([_b("s", "exit", summary=long_sum)], 0)
    assert len(embed.fields[0].value) < 200


# ---- summary written at completion -----------------------------------------


class _Bot:
    def __init__(self):
        self.settings = _Settings()

    def get_channel(self, _id):
        return None

    async def fetch_channel(self, _id):
        return None

    def is_ready(self):
        return True


class _Progress:
    async def done(self, binding):
        pass


async def test_notify_complete_stores_summary(tmp_path):
    db = await _db(tmp_path)
    b = Binding(session_id="s1", thread_id=10, channel_id=5,
                status="running")
    await db.upsert_binding(b)
    r = Relay(_Bot(), None, db, _Settings())  # type: ignore[arg-type]
    r.progress = _Progress()  # type: ignore[assignment]
    chan = _Chan()

    async def _t(_b):
        return chan

    r._thread = _t
    sess = Session(
        session_id="s1", url="u", status="exit",
        structured_output={"summary": "shipped the thing"},
    )
    await r._notify(b, sess, Notification("complete", "Session finished."))
    assert b.summary == "shipped the thing"
    # the tick-end upsert carries it — persisted, digest-readable
    await db.upsert_binding(b)
    got = await db.get_binding("s1")
    assert got is not None and got.summary == "shipped the thing"
    # a state-only re-upsert doesn't wipe it
    got.status = "exit"
    got.summary = ""
    await db.upsert_binding(got)
    assert (await db.get_binding("s1")).summary == "shipped the thing"  # type: ignore[union-attr]


# ---- digest-kind schedule ----------------------------------------------------


class _SchedBot(_Bot):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.hub = _Chan()

    def get_channel(self, _id):
        return self.hub


async def test_digest_schedule_fires_rollup(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    now = int(time.time())
    row = ScheduleRow(
        id=0, prompt="digest", interval_seconds=86400,
        next_run_at=now - 10, kind="digest",
    )
    row.id = await db.add_schedule(row)
    bot = _SchedBot(db)
    called = []

    async def _boom(*a, **kw):
        called.append(kw)
        raise AssertionError("digest rows must not spawn")

    monkeypatch.setattr(sched_mod, "spawn_session", _boom)
    await Scheduler(bot)._fire_due()  # type: ignore[arg-type]
    assert not called
    embeds = [kw["embed"] for _, kw in bot.hub.sent if "embed" in kw]
    assert len(embeds) == 1 and "digest" in embeds[0].title.lower()
    # next_run_at slid forward from fire-time
    rows = await db.all_schedules()
    assert rows[0].next_run_at > now


async def test_digest_schedule_failure_still_advances(tmp_path):
    """A hub channel that can't send must not leave the row due — that
    would retry (and log-spam) every tick forever."""
    db = await _db(tmp_path)
    now = int(time.time())
    row = ScheduleRow(
        id=0, prompt="digest", interval_seconds=86400,
        next_run_at=now - 10, kind="digest",
    )
    row.id = await db.add_schedule(row)

    class _BrokenHub(_Chan):
        async def send(self, *a, **kw):
            raise RuntimeError("hub channel deleted")

    bot = _SchedBot(db)
    bot.hub = _BrokenHub()
    await Scheduler(bot)._fire_due()  # type: ignore[arg-type]
    assert (await db.all_schedules())[0].next_run_at > now


async def test_spawn_schedule_still_spawns(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    now = int(time.time())
    row = ScheduleRow(
        id=0, prompt="do thing", interval_seconds=3600,
        next_run_at=now - 10,
    )
    row.id = await db.add_schedule(row)

    async def _fake_spawn(bot, **kw):
        class _T:
            async def send(self, *a, **kw):
                pass
        return Session(session_id="sx", url="u", status="running"), _T()

    monkeypatch.setattr(sched_mod, "spawn_session", _fake_spawn)
    await Scheduler(_SchedBot(db))._fire_due()  # type: ignore[arg-type]
    assert (await db.all_schedules())[0].last_session_id == "sx"


async def test_spawn_schedule_failure_posts_hub_notice(
    tmp_path, monkeypatch
):
    """A SpawnError (quota, missing hub config) still advances the row —
    and now posts a hub notice so the owner sees the skip instead of
    'nothing due'."""
    from devinmobile.spawn import SpawnError

    db = await _db(tmp_path)
    now = int(time.time())
    row = ScheduleRow(
        id=0, prompt="do thing", interval_seconds=3600,
        next_run_at=now - 10,
    )
    row.id = await db.add_schedule(row)

    async def _boom(bot, **kw):
        raise SpawnError("daily ACU quota 5 reached")

    monkeypatch.setattr(sched_mod, "spawn_session", _boom)
    bot = _SchedBot(db)
    await Scheduler(bot)._fire_due()  # type: ignore[arg-type]
    texts = [str(a[0]) for a, _ in bot.hub.sent if a]
    assert any("skipped" in t and "quota" in t for t in texts), texts
    assert (await db.all_schedules())[0].next_run_at > now

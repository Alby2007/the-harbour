"""Monitor schedules — run_check probes (URL + ci:) and the _fire_monitor
edge/cooldown/still-running state machine."""

import time

import discord
import httpx

import devinmobile.monitors as mon
import devinmobile.scheduler as sched_mod
from devinmobile.db import Binding, Database, ScheduleRow
from devinmobile.models import Session
from devinmobile.monitors import Check, parse_watch, run_check
from devinmobile.scheduler import Scheduler


async def _db(tmp_path) -> Database:
    return await Database.connect(str(tmp_path / "t.db"))


# ---- parse_watch ------------------------------------------------------------


def test_parse_watch():
    assert parse_watch("https://x/health") == ("url", "https://x/health")
    assert parse_watch("ci:o/r") == ("ci", "o", "r", "HEAD")
    assert parse_watch("ci:o/r@main") == ("ci", "o", "r", "main")
    assert parse_watch("nonsense") is None
    assert parse_watch("") is None


# ---- run_check: URL ----------------------------------------------------------


class _Resp:
    def __init__(self, status=200, text="ok"):
        self.status_code = status
        self._text = text
        self.charset_encoding = "utf-8"

    async def aiter_bytes(self, _n):
        yield self._text.encode()


class _FakeStream:
    """client.stream() returns an async ctx mgr, not a coroutine."""
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc = resp, exc

    async def __aenter__(self):
        if self.exc:
            raise self.exc
        return self.resp

    async def __aexit__(self, *a):
        return None


class _FakeHttp:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc = resp, exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    def stream(self, *a, **kw):
        return _FakeStream(self.resp, self.exc)


def _http(monkeypatch, resp=None, exc=None):
    monkeypatch.setattr(
        mon.httpx, "AsyncClient",
        lambda *a, **kw: _FakeHttp(resp, exc),
    )


async def test_url_ok_and_expect(monkeypatch):
    _http(monkeypatch, _Resp(200, "version 1.2 ok"))
    r = await run_check("https://x/health", "ok", None)
    assert r.state == "ok"
    r = await run_check("https://x/health", "missing-string", None)
    assert r.state == "red" and "missing" in r.detail


async def test_url_non2xx_and_down(monkeypatch):
    _http(monkeypatch, _Resp(503, "down"))
    r = await run_check("https://x/", "", None)
    assert r.state == "red" and "HTTP 503" in r.detail
    # transport failure IS the signal — a down endpoint reads red, and a
    # bot-host network outage reads the same (bounded by edge+cooldown)
    _http(monkeypatch, exc=httpx.ConnectError("refused"))
    assert (await run_check("https://x/", "", None)).state == "red"
    _http(monkeypatch, exc=httpx.ReadTimeout("slow"))
    r = await run_check("https://x/", "", None)
    assert r.state == "red" and "timeout" in r.detail


# ---- run_check: ci: ------------------------------------------------------------


class _GH:
    def __init__(self, state=None, exc=None):
        self.state, self.exc = state, exc

    async def get_ref_checks(self, o, r, ref):
        if self.exc:
            raise self.exc
        return self.state


async def test_ci_states(monkeypatch):
    red = await run_check("ci:o/r@main", "", _GH("failure"))
    assert red.state == "red"
    for s in ("pending", "success", "none"):
        assert (await run_check("ci:o/r", "", _GH(s))).state == "ok"
    # GitHub API outage is unknown, never red — no spawn on a flaky API
    r = await run_check("ci:o/r", "", _GH(exc=RuntimeError("500")))
    assert r.state == "unknown"
    assert (await run_check("ci:o/r", "", None)).state == "unknown"


async def test_malformed_watch_unknown():
    assert (await run_check("wat", "", None)).state == "unknown"


# ---- _fire_monitor state machine -----------------------------------------------


class _Settings:
    hub_channel_id = 42


class _Chan(discord.abc.Messageable):
    __slots__ = ("sent",)

    def __init__(self):
        self.sent: list = []

    def _get_channel(self):
        return self

    async def send(self, *a, **kw):
        self.sent.append((a, kw))


class _Bot:
    def __init__(self, db, github=None):
        self.db = db
        self.settings = _Settings()
        self.github = github
        self.hub = _Chan()

    def get_channel(self, _id):
        return self.hub

    async def fetch_channel(self, _id):
        return self.hub


async def _monitor_row(db, **kw) -> ScheduleRow:
    now = int(time.time())
    row = ScheduleRow(
        id=0, prompt="fix the thing", interval_seconds=3600,
        next_run_at=now - 10, kind="monitor",
        watch="https://x/health", **kw,
    )
    row.id = await db.add_schedule(row)
    return row


def _stub_check(monkeypatch, state, detail="boom"):
    async def _check(watch, expect, github):
        return Check(state, detail)
    monkeypatch.setattr(sched_mod, "run_check", _check)


def _stub_spawn(monkeypatch, calls):
    async def _spawn(bot, **kw):
        calls.append(kw)

        class _T:
            async def send(self, *a, **kw):
                pass

        return Session(session_id="fix1", url="u", status="running"), _T()
    monkeypatch.setattr(sched_mod, "spawn_session", _spawn)


async def test_monitor_red_edge_fires(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    await _monitor_row(db)
    _stub_check(monkeypatch, "red", "HTTP 503")
    calls: list = []
    _stub_spawn(monkeypatch, calls)
    await Scheduler(_Bot(db))._fire_due()  # type: ignore[arg-type]
    assert len(calls) == 1
    # the fix prompt carries the evidence block — human timestamp, not
    # Discord markup Devin can't render
    assert "Monitor tripped" in calls[0]["prompt"]
    assert "HTTP 503" in calls[0]["prompt"]
    assert "<t:" not in calls[0]["prompt"]
    assert "UTC" in calls[0]["prompt"]
    assert calls[0]["title"] == "[monitor] https://x/health"
    row = (await db.all_schedules())[0]
    assert row.watch_state == "red"
    assert row.last_fired_at is not None
    assert row.last_session_id == "fix1"


async def test_monitor_still_red_suppressed(tmp_path, monkeypatch):
    """A persistently-down target can't spawn-storm: already-red, inside
    cooldown → no spawn."""
    db = await _db(tmp_path)
    await _monitor_row(
        db, watch_state="red", last_fired_at=int(time.time()) - 60
    )
    _stub_check(monkeypatch, "red")
    calls: list = []
    _stub_spawn(monkeypatch, calls)
    await Scheduler(_Bot(db))._fire_due()  # type: ignore[arg-type]
    assert not calls
    assert (await db.all_schedules())[0].watch_state == "red"


async def test_monitor_cooldown_refires(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    await _monitor_row(
        db, watch_state="red",
        last_fired_at=int(time.time()) - 20000, cooldown_seconds=14400,
    )
    _stub_check(monkeypatch, "red")
    calls: list = []
    _stub_spawn(monkeypatch, calls)
    await Scheduler(_Bot(db))._fire_due()  # type: ignore[arg-type]
    assert len(calls) == 1


async def test_monitor_still_running_suppresses(tmp_path, monkeypatch):
    """The previous fix session still polling → no re-spawn, even past
    cooldown."""
    db = await _db(tmp_path)
    await db.upsert_binding(
        Binding(session_id="fix0", thread_id=1, channel_id=5, active=True)
    )
    await _monitor_row(
        db, watch_state="red",
        last_fired_at=int(time.time()) - 99999, cooldown_seconds=14400,
        last_session_id="fix0",
    )
    _stub_check(monkeypatch, "red")
    calls: list = []
    _stub_spawn(monkeypatch, calls)
    await Scheduler(_Bot(db))._fire_due()  # type: ignore[arg-type]
    assert not calls
    # last_session_id survived the tick — it's the dedup key
    assert (await db.all_schedules())[0].last_session_id == "fix0"


async def test_monitor_green_recovery_posts(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    await _monitor_row(db, watch_state="red")
    _stub_check(monkeypatch, "ok")
    calls: list = []
    _stub_spawn(monkeypatch, calls)
    bot = _Bot(db)
    await Scheduler(bot)._fire_due()  # type: ignore[arg-type]
    assert not calls
    assert any("recovered" in str(a) for a, _ in bot.hub.sent)
    assert (await db.all_schedules())[0].watch_state == "green"


async def test_monitor_unknown_no_spawn(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    await _monitor_row(db)
    _stub_check(monkeypatch, "unknown", "github api: 500")
    calls: list = []
    _stub_spawn(monkeypatch, calls)
    await Scheduler(_Bot(db))._fire_due()  # type: ignore[arg-type]
    assert not calls


async def test_monitor_spawn_failure_marks_red(tmp_path, monkeypatch):
    """A broken spawn path records red+the attempt — cooldown becomes the
    retry floor instead of a fresh edge every interval."""
    from devinmobile.spawn import SpawnError

    db = await _db(tmp_path)
    await _monitor_row(db)
    _stub_check(monkeypatch, "red")

    async def _boom(bot, **kw):
        raise SpawnError("HUB_CHANNEL_ID is not configured.")

    monkeypatch.setattr(sched_mod, "spawn_session", _boom)
    await Scheduler(_Bot(db))._fire_due()  # type: ignore[arg-type]
    row = (await db.all_schedules())[0]
    assert row.watch_state == "red"
    assert row.last_fired_at is not None
    assert not row.last_session_id  # nothing spawned


async def test_monitor_send_failure_no_restrike(tmp_path, monkeypatch):
    """thread.send raising after spawn must not re-edge the monitor —
    the spawn is still recorded so the next tick is suppressed."""
    db = await _db(tmp_path)
    await _monitor_row(db)
    _stub_check(monkeypatch, "red")
    calls: list = []

    async def _spawn(bot, **kw):
        calls.append(kw)

        class _T:
            async def send(self, *a, **kw):
                raise RuntimeError("thread deleted mid-send")

        return Session(session_id="fix1", url="u", status="running"), _T()

    monkeypatch.setattr(sched_mod, "spawn_session", _spawn)
    sched = Scheduler(_Bot(db))
    await sched._fire_due()
    assert len(calls) == 1
    row = (await db.all_schedules())[0]
    # the spawn IS recorded despite the provenance post crashing —
    # watch_state red + last_fired_at + last_session_id all landed
    assert row.watch_state == "red"
    assert row.last_fired_at is not None
    assert row.last_session_id == "fix1"
    # next tick inside cooldown: not an edge, not cooled → suppressed
    calls.clear()
    await sched._fire_monitor(row, int(time.time()))
    assert not calls


async def test_monitor_recovery_post_failure_still_greens(
    tmp_path, monkeypatch
):
    """A dead hub channel can't wedge watch_state at 'red' — the state
    flip lands before the recovery post."""
    db = await _db(tmp_path)
    await _monitor_row(db, watch_state="red")
    _stub_check(monkeypatch, "ok")

    class _DeadHub(_Chan):
        async def send(self, *a, **kw):
            raise RuntimeError("channel deleted")

    bot = _Bot(db)
    bot.hub = _DeadHub()
    await Scheduler(bot)._fire_due()  # type: ignore[arg-type]
    assert (await db.all_schedules())[0].watch_state == "green"


# ---- /schedule monitor validation ---------------------------------------------


class _Tree:
    def __init__(self):
        self.commands: dict = {}

    def command(self, **kw):
        def deco(fn):
            self.commands[kw.get("name")] = fn
            return fn
        return deco


class _Resp2:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, content=None, **kw):
        self.sent.append(content if content is not None else str(kw))


class _Ix:
    def __init__(self, uid=1):
        self.user = type("U", (), {"id": uid})()
        self.response = _Resp2()


async def test_schedule_monitor_validation(tmp_path):
    from devinmobile.bot.commands import register_commands

    db = await _db(tmp_path)
    bot = type("B", (), {})()
    bot.tree = _Tree()
    bot.db = db
    bot.settings = type(
        "S", (), {
            "allowed_user_id_set": {1},
            "max_acu_limit": 25,
            "github_enabled": False,
            "admin_user_id_set": frozenset(),
            "is_operator": (
                lambda self, uid, role_ids=():
                uid in self.allowed_user_id_set
            ),
        },
    )()
    register_commands(bot)
    cmd = bot.tree.commands["schedule"]

    ix = _Ix()
    await cmd(ix, every="30m", prompt="fix it", kind="monitor",
              watch="not-a-watch")
    assert "watch:" in ix.response.sent[0]

    ix = _Ix()
    await cmd(ix, every="30m", prompt="fix it", kind="monitor",
              watch="ci:o/r")
    assert "GitHub App" in ix.response.sent[0]

    ix = _Ix()
    await cmd(ix, every="30m", kind="monitor", watch="https://x/health")
    assert "prompt:" in ix.response.sent[0]

    ix = _Ix()
    await cmd(ix, every="30m", prompt="p", kind="monitor",
              watch="ci:o/r", expect="ok")
    assert "expect:" in ix.response.sent[0]

    ix = _Ix()
    await cmd(ix, every="30m", prompt="p", watch="https://x")
    assert "kind:monitor" in ix.response.sent[0]

    # happy path
    ix = _Ix()
    await cmd(ix, every="30m", prompt="fix the thing", kind="monitor",
              watch="https://x/health", expect="ok", cooldown="8h")
    assert "Scheduled" in ix.response.sent[0]
    row = (await db.all_schedules())[0]
    assert row.kind == "monitor" and row.watch == "https://x/health"
    assert row.expect == "ok" and row.cooldown_seconds == 28800

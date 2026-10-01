"""Repo memory — completion harvest → repo_notes rows, spawn-time prompt
injection, and the /note /notes /unnote command trio."""

import devinmobile.spawn as spawn_mod
from devinmobile.bot.commands import register_commands
from devinmobile.db import Binding, Database
from devinmobile.models import Session
from devinmobile.relay import Relay


class _Settings:
    allowed_user_id_set = {1}
    max_acu_limit = 25
    auto_respawn = True
    silence_alert_minutes = 0
    create_as_user_id = None
    github_merge_method = "squash"
    hub_channel_id = 42
    default_model = None
    devin_mode = "normal"


class _Chan:
    def __init__(self):
        self.id = 777
        self.sent: list[str] = []

    @property
    def mention(self):
        return "<#99>"

    async def send(self, *a, **kw):
        self.sent.append(a[0] if a else kw.get("content") or "")
        return _Msg()

    async def join(self):
        pass


class _Msg:
    id = 900


class _Bot:
    def __init__(self):
        self.settings = _Settings()

    def get_channel(self, _id):
        return None

    async def fetch_channel(self, _id):
        return None

    def is_ready(self):
        return True


async def _db(tmp_path) -> Database:
    return await Database.connect(str(tmp_path / "t.db"))


def _sess(**kw) -> Session:
    kw.setdefault("status", "exit")
    return Session(session_id="s1", url="https://x/s1", **kw)


# ---- harvest ------------------------------------------------------------


async def test_harvest_writes_notes_and_posts(tmp_path):
    db = await _db(tmp_path)
    b = Binding(session_id="s1", thread_id=10, channel_id=5, repos="o/r")
    r = Relay(_Bot(), None, db, _Settings())  # type: ignore[arg-type]
    chan = _Chan()

    async def _t(_b):
        return chan

    r._thread = _t  # shadow the isinstance-checked method
    sess = _sess(structured_output={
        "repo_notes": ["pytest is flaky — use -x", "needs .env.test", "", 7],
    })
    await r._harvest_repo_notes(b, sess)
    notes = await db.list_notes("o/r")
    assert sorted(n[2] for n in notes) == [
        "needs .env.test", "pytest is flaky — use -x",
    ]
    assert any("Saved 2 repo notes" in s for s in chan.sent)
    # harvest is idempotent — UNIQUE(repo, note) dedupes a re-run
    chan.sent.clear()
    await r._harvest_repo_notes(b, sess)
    assert not chan.sent


async def test_harvest_silent_when_absent(tmp_path):
    db = await _db(tmp_path)
    b = Binding(session_id="s1", thread_id=10, channel_id=5, repos="o/r")
    r = Relay(_Bot(), None, db, _Settings())  # type: ignore[arg-type]
    chan = _Chan()

    async def _t(_b):
        return chan

    r._thread = _t
    await r._harvest_repo_notes(b, _sess(structured_output=None))
    await r._harvest_repo_notes(b, _sess(structured_output={}))
    assert not chan.sent
    # no repos on the binding — nothing to key notes against
    b2 = Binding(session_id="s2", thread_id=11, channel_id=5)
    await r._harvest_repo_notes(
        b2, _sess(structured_output={"repo_notes": ["x"]})
    )
    assert await db.list_notes() == []
    assert not chan.sent


# ---- spawn injection --------------------------------------------------------


class _Hub:
    """Stands in for a discord.TextChannel (monkeypatched isinstance)."""

    id = 42

    def __init__(self):
        self.threads: list[tuple[str, _Chan]] = []

    async def create_thread(self, name, type=None):
        t = _Chan()
        self.threads.append((name, t))
        return t


class _Devin:
    def __init__(self):
        self.created: list[dict] = []

    async def create_session(self, **kw):
        self.created.append(kw)
        return Session(session_id="s9", url="u9", status="running")


class _Progress:
    async def thinking(self, binding):
        pass


class _RelayStub:
    def __init__(self):
        self.progress = _Progress()
        self.polled: list[str] = []

    def request_poll(self, binding):
        self.polled.append(binding.session_id)


class _SpawnBot(_Bot):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.devin = _Devin()
        self.relay = _RelayStub()
        self.bridge = type("B", (), {"available": False})()
        self.hub = _Hub()

    def get_channel(self, _id):
        return self.hub

    async def fetch_channel(self, _id):
        return self.hub

    async def handle_component(self, *a):
        pass


async def test_spawn_injects_repo_notes(tmp_path, monkeypatch):
    import discord

    db = await _db(tmp_path)
    await db.add_note("o/r", "pytest is flaky — use -x", "u1")
    bot = _SpawnBot(db)
    monkeypatch.setattr(discord, "TextChannel", _Hub)
    await spawn_mod.spawn_session(bot, prompt="do work", repos=["o/r"])
    prompt = bot.devin.created[0]["prompt"]
    assert "Repo notes" in prompt and "pytest is flaky — use -x" in prompt
    assert "structured_output.repo_notes" in prompt  # the harvest nudge


async def test_spawn_without_notes_still_nudges(tmp_path, monkeypatch):
    import discord

    db = await _db(tmp_path)
    bot = _SpawnBot(db)
    monkeypatch.setattr(discord, "TextChannel", _Hub)
    await spawn_mod.spawn_session(bot, prompt="do work", repos=["o/r"])
    prompt = bot.devin.created[0]["prompt"]
    assert "Repo notes" not in prompt
    assert "structured_output.repo_notes" in prompt


# ---- /note /notes /unnote ----------------------------------------------------


class _Tree:
    def __init__(self):
        self.commands: dict = {}

    def command(self, **kw):
        def deco(fn):
            self.commands[kw.get("name")] = fn
            return fn

        return deco


class _Resp:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, content=None, **kw):
        self.sent.append(content if content is not None else str(kw))


class _FakeThread:
    def __init__(self, tid=10):
        self.id = tid


class _Ix:
    def __init__(self, uid=1, channel=None):
        self.user = type("U", (), {"id": uid})()
        self.response = _Resp()
        self.channel = channel


def _command_bot(db):
    bot = type("B", (), {})()
    bot.tree = _Tree()
    bot.settings = _Settings()
    bot.db = db
    register_commands(bot)
    return bot


async def test_note_thread_fallback_and_gate(tmp_path, monkeypatch):
    import discord

    db = await _db(tmp_path)
    await db.upsert_binding(Binding(
        session_id="s1", thread_id=10, channel_id=5, repos="o/r,o/r2",
    ))
    bot = _command_bot(db)
    monkeypatch.setattr(discord, "Thread", _FakeThread)

    # allowlist gate first
    ix = _Ix(uid=99, channel=_FakeThread())
    await bot.tree.commands["note"](ix, text="x")
    assert "locked to its owner" in ix.response.sent[0]

    # inside a bound thread, repo falls back to the binding's first repo
    ix = _Ix(channel=_FakeThread())
    await bot.tree.commands["note"](ix, text="pytest -x or bust")
    assert "o/r" in ix.response.sent[0]
    notes = await db.list_notes("o/r")
    assert notes and notes[0][2] == "pytest -x or bust"

    # explicit repo wins over the thread fallback
    ix = _Ix(channel=_FakeThread())
    await bot.tree.commands["note"](ix, text="other", repo="a/b")
    assert (await db.list_notes("a/b"))[0][2] == "other"

    # unresolvable repo → ephemeral nudge
    ix = _Ix(channel=None)
    await bot.tree.commands["note"](ix, text="x")
    assert "Pass `repo:`" in ix.response.sent[0]


async def test_notes_and_unnote(tmp_path):
    db = await _db(tmp_path)
    await db.add_note("o/r", "n1", "u1")
    await db.add_note("o/r", "n1", "u2")  # dupe — ignored
    bot = _command_bot(db)

    ix = _Ix()
    await bot.tree.commands["notes"](ix, repo="O/R")  # case-insensitive
    assert ix.response.sent  # an embed went back (contents live in the db)
    rows = await db.list_notes("o/r")
    assert len(rows) == 1 and rows[0][2] == "n1"

    ix = _Ix()
    await bot.tree.commands["unnote"](ix, note_id=rows[0][0])
    assert "Deleted" in ix.response.sent[0]
    assert await db.list_notes("o/r") == []

    ix = _Ix()
    await bot.tree.commands["unnote"](ix, note_id=999)
    assert "No note #999" in ix.response.sent[0]

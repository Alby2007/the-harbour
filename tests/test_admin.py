"""Session admin — /delete soft-delete, /rename (Discord-side), /kill
honest terminate reporting, and the deleted-binding steering guard."""

from types import SimpleNamespace

import discord

from devinmobile.bot.commands import register_commands
from devinmobile.bot.main import DevinMobileBot
from devinmobile.db import Binding, Database
from devinmobile.embeds import status_embed
from devinmobile.models import Session


async def _db(tmp_path) -> Database:
    return await Database.connect(str(tmp_path / "t.db"))


# ---- db: deleted column --------------------------------------------------------


def _b(sid, **kw) -> Binding:
    kw.setdefault("thread_id", 1)
    kw.setdefault("channel_id", 5)
    return Binding(session_id=sid, **kw)


async def test_deleted_roundtrip_and_filters(tmp_path):
    db = await _db(tmp_path)
    await db.upsert_binding(_b("live", title="live"))
    await db.upsert_binding(_b("dead", title="dead", deleted=True))

    # listings hide it
    assert [b.session_id for b in await db.all_bindings()] == ["live"]
    # accounting still counts it
    ids = {b.session_id for b in await db.all_bindings(include_deleted=True)}
    assert ids == {"live", "dead"}
    # inbox gather excludes it (even with an inbox-worthy status)
    await db.upsert_binding(_b("dead", deleted=True, status="error"))
    inbox = await db.inbox_bindings()
    assert all(b.session_id != "dead" for b in inbox)
    # but the row itself still resolves (steering guard reads it)
    dead = await db.get_binding("dead")
    assert dead is not None and dead.deleted


async def test_deleted_is_write_once(tmp_path):
    """A state-only upsert (deleted=False) can't resurrect a deleted
    binding — same convention as spawned_by's NULLIF preserve."""
    db = await _db(tmp_path)
    await db.upsert_binding(_b("s1", deleted=True))
    await db.upsert_binding(_b("s1", status="error"))  # deleted omitted
    b = await db.get_binding("s1")
    assert b is not None and b.deleted


# ---- command harness ------------------------------------------------------------


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
        self.sent.append(str(text))

    async def send(self, text="", **kw):
        await self.send_message(text, **kw)

    async def defer(self, **kw):
        self.deferred = True

    def is_done(self):
        return bool(self.sent) or self.deferred


class _FakeThread:
    """isinstance target (monkeypatched over discord.Thread)."""

    def __init__(self):
        self.sent: list[str] = []
        self.archived = False
        self.deleted = False
        self.name = ""

    async def send(self, text, **kw):
        self.sent.append(text)

    async def edit(self, **kw):
        if "archived" in kw:
            self.archived = kw["archived"]
        if "name" in kw:
            self.name = kw["name"]

    async def delete(self):
        self.deleted = True


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


def _bot(db, **over):
    bot = SimpleNamespace(tree=_Tree(), db=db)
    bot.settings = type(
        "S", (), {
            "allowed_user_id_set": {1, 2, 9},
            "admin_user_id_set": frozenset({9}),
            "required_role_id": None,
            "is_operator": (
                lambda self, uid, role_ids=():
                uid in self.allowed_user_id_set
            ),
            "is_admin": lambda self, uid: uid in self.admin_user_id_set,
        },
    )()
    bot.devin = SimpleNamespace(
        terminate_session=lambda sid: None,
    )
    bot.__dict__.update(over)
    register_commands(bot)
    return bot


async def _seed(db, spawned_by="1"):
    await db.upsert_binding(_b(
        "s1", title="old title", spawned_by=spawned_by,
        status="running",
    ))


# ---- /kill honest reporting -------------------------------------------------------


async def test_kill_reports_terminated(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    await _seed(db)
    bot = _bot(db)

    async def _term(sid):
        pass  # API said yes

    bot.devin = SimpleNamespace(terminate_session=_term)
    thread = _FakeThread()

    async def _fetch(cid):
        return thread

    ix = _Ix(uid=1, fetch_channel=_fetch)
    monkeypatch.setattr(discord, "Thread", _FakeThread)
    await bot.tree.commands["kill"](ix, session="s1")
    assert "terminated" in ix.followup.sent[0]
    assert "Parked" in ix.followup.sent[0]
    assert thread.archived


async def test_kill_reports_api_refusal(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    await _seed(db)
    bot = _bot(db)

    async def _term(sid):
        raise RuntimeError("405 Method Not Allowed")

    bot.devin = SimpleNamespace(terminate_session=_term)

    async def _fetch(cid):
        return _FakeThread()

    ix = _Ix(uid=1, fetch_channel=_fetch)
    monkeypatch.setattr(discord, "Thread", _FakeThread)
    await bot.tree.commands["kill"](ix, session="s1")
    assert "refused" in ix.followup.sent[0]
    assert "idle-suspend" in ix.followup.sent[0]
    # local park still happened — the binding is inactive either way
    assert not (await db.get_binding("s1")).active


# ---- /delete ------------------------------------------------------------------


async def test_delete_marks_hidden_and_archives(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    await _seed(db)
    bot = _bot(db)

    async def _term(sid):
        pass

    bot.devin = SimpleNamespace(terminate_session=_term)
    thread = _FakeThread()

    async def _fetch(cid):
        return thread

    ix = _Ix(uid=1, fetch_channel=_fetch)
    monkeypatch.setattr(discord, "Thread", _FakeThread)
    await bot.tree.commands["delete"](ix, session="s1")
    b = await db.get_binding("s1")
    assert b is not None and b.deleted and not b.active
    assert thread.archived and not thread.deleted
    assert "terminated" in ix.followup.sent[0]
    # hidden from listings, counted by accounting
    assert await db.all_bindings() == []
    assert len(await db.all_bindings(include_deleted=True)) == 1


async def test_delete_wipe_removes_thread(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    await _seed(db)
    bot = _bot(db)
    bot.devin = SimpleNamespace(terminate_session=lambda sid: None)
    thread = _FakeThread()

    async def _fetch(cid):
        return thread

    ix = _Ix(uid=1, fetch_channel=_fetch)
    monkeypatch.setattr(discord, "Thread", _FakeThread)
    await bot.tree.commands["delete"](ix, session="s1", wipe=True)
    assert thread.deleted and not thread.archived
    assert "thread removed" in ix.followup.sent[0]


async def test_delete_owner_gate(tmp_path):
    db = await _db(tmp_path)
    await _seed(db, spawned_by="2")
    bot = _bot(db)
    ix = _Ix(uid=1)  # non-owner, non-admin
    await bot.tree.commands["delete"](ix, session="s1")
    assert "owner or admin" in ix.response.sent[0]
    assert not (await db.get_binding("s1")).deleted


async def test_delete_in_thread_resolves_binding(tmp_path, monkeypatch):
    """session: omitted → the current thread's binding (same as /kill)."""
    db = await _db(tmp_path)
    await _seed(db)
    bot = _bot(db)
    bot.devin = SimpleNamespace(terminate_session=lambda sid: None)
    thread = _FakeThread()

    async def _fetch(cid):
        return thread

    ix = _Ix(uid=1, channel=_FakeThread(), fetch_channel=_fetch)
    ix.channel.id = 1  # _b default thread_id — get_binding_by_thread hits
    monkeypatch.setattr(discord, "Thread", _FakeThread)
    await bot.tree.commands["delete"](ix)
    assert (await db.get_binding("s1")).deleted


# ---- /rename -----------------------------------------------------------------


async def test_rename_updates_thread_and_binding(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    await _seed(db)
    bot = _bot(db)
    thread = _FakeThread()

    async def _fetch(cid):
        return thread

    ix = _Ix(uid=1, fetch_channel=_fetch)
    monkeypatch.setattr(discord, "Thread", _FakeThread)
    await bot.tree.commands["rename"](ix, title="better name", session="s1")
    assert thread.name == "better name"
    assert (await db.get_binding("s1")).title == "better name"
    assert "Renamed" in ix.response.sent[0]


def test_status_embed_binding_title_wins():
    """/rename must be VISIBLE — a user-set title beats Devin's
    auto-title on the embed."""
    sess = Session(
        session_id="s1", url="u", status="running", title="auto-generated"
    )
    embed = status_embed(sess, fallback_title="My name")
    assert embed.title == "My name"
    # but when no local title is set, the session title still shows
    embed = status_embed(sess, fallback_title=None)
    assert embed.title == "auto-generated"


# ---- deleted binding steering guard -----------------------------------------------


async def test_on_message_ignores_deleted_binding(tmp_path):
    """A stray reply in a deleted session's thread must not reactivate
    it — the lookup returns the row, the guard drops the message."""
    db = await _db(tmp_path)
    await db.upsert_binding(_b("s1", deleted=True, active=True))
    bot = DevinMobileBot.__new__(DevinMobileBot)
    bot.db = db
    bot.settings = SimpleNamespace(
        allowed_user_id_set={1},
        is_operator=lambda *a, **kw: True,
    )
    calls: list = []

    async def _send(*a, **kw):
        calls.append(a)

    bot.devin = SimpleNamespace(send_message=_send)
    msg = SimpleNamespace(
        author=SimpleNamespace(id=1, bot=False),
        content="hello",
        attachments=[],
        guild=object(),          # guild path, not a DM
        channel=SimpleNamespace(id=1),
        reference=None,
    )
    await bot.on_message(msg)
    assert not calls  # deleted binding → no steering, no reactivation

"""Native playbooks + org secrets — resolve_playbook name matching, the
/playbook* and /secrets command layer, and spawn's v3-only gate."""

from types import SimpleNamespace

import discord
import pytest

import devinmobile.spawn as spawn_mod
from devinmobile.bot.commands import register_commands, resolve_playbook
from devinmobile.db import Database
from devinmobile.models import Session
from devinmobile.spawn import SpawnError

BOOKS = [
    {"playbook_id": "pb-1", "title": "Janitor", "body": "clean stuff"},
    {"playbook_id": "pb-2", "title": "Nightly audit", "body": "audit"},
    {"playbook_id": "pb-3", "title": "Nightly triage", "body": "triage"},
]


# ---- resolve_playbook -----------------------------------------------------


def test_resolve_playbook_exact_then_substring():
    assert resolve_playbook("janitor", BOOKS)["playbook_id"] == "pb-1"
    assert resolve_playbook("JANITOR", BOOKS)["playbook_id"] == "pb-1"
    # unambiguous substring
    assert resolve_playbook("audit", BOOKS)["playbook_id"] == "pb-2"
    # ambiguous substring → the hit list, not a coin flip
    hits = resolve_playbook("nightly", BOOKS)
    assert isinstance(hits, list) and len(hits) == 2
    assert resolve_playbook("nope", BOOKS) is None
    assert resolve_playbook("", BOOKS) is None


# ---- command harness -------------------------------------------------------


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
        self.sent: list = []
        self.deferred = False
        self.modal = None

    async def send_message(self, content=None, **kw):
        self.sent.append(content if content is not None else kw)

    async def send(self, content=None, **kw):
        await self.send_message(content, **kw)

    async def defer(self, **kw):
        self.deferred = True

    async def send_modal(self, modal):
        self.modal = modal


class _Ix:
    def __init__(self, uid=1):
        self.user = type("U", (), {"id": uid})()
        self.response = _Resp()
        self.followup = _Resp()
        self.channel = None


class _Devin:
    def __init__(self):
        self.books = list(BOOKS)
        self.secrets = [
            {"secret_id": "sec-1", "key": "GH_PAT",
             "secret_type": "key-value", "note": "ci push"},
        ]
        self.created_books: list = []
        self.updated_books: list = []
        self.deleted_books: list = []
        self.deleted_secrets: list = []

    async def list_playbooks(self):
        return list(self.books)

    async def create_playbook(self, title, body):
        self.created_books.append({"title": title, "body": body})
        return {"playbook_id": "pb-new"}

    async def update_playbook(self, pid, *, title, body):
        self.updated_books.append({"id": pid, "title": title, "body": body})

    async def delete_playbook(self, pid):
        self.deleted_books.append(pid)

    async def list_secrets(self):
        return list(self.secrets)

    async def delete_secret_by_key(self, key):
        for s in self.secrets:
            if s["key"] == key:
                self.secrets.remove(s)
                self.deleted_secrets.append(key)
                return True
        return False


def _bot(db=None, admin_set=frozenset({9})):
    bot = SimpleNamespace(tree=_Tree(), db=db)
    bot.settings = type(
        "S", (), {
            "allowed_user_id_set": {1, 2, 9},
            "admin_user_id_set": admin_set,
            "required_role_id": None,
            "is_operator": (
                lambda self, uid, role_ids=():
                uid in self.allowed_user_id_set
            ),
            "is_admin": lambda self, uid: uid in self.admin_user_id_set,
        },
    )()
    bot.devin = _Devin()
    register_commands(bot)
    return bot


# ---- /secrets + /unsecret ---------------------------------------------------


async def test_secrets_lists_metadata_only():
    bot = _bot()
    ix = _Ix()
    await bot.tree.commands["secrets"](ix)
    # embed went back ephemeral — the fake records the kw dict
    embed = ix.response.sent[0]["embed"]
    text = embed.fields[0].name + embed.fields[0].value
    assert "GH_PAT" in text and "key-value" in text and "ci push" in text
    # the API serves no value field, so the render only has metadata —
    # 'never Discord' for secrets is enforced by the surface itself


async def test_unsecret_admin_gate():
    bot = _bot()
    ix = _Ix(uid=9)  # admin
    await bot.tree.commands["unsecret"](ix, name="GH_PAT")
    assert "Deleted" in ix.followup.sent[0]
    assert bot.devin.deleted_secrets == ["GH_PAT"]

    ix = _Ix(uid=1)  # operator, not admin
    await bot.tree.commands["unsecret"](ix, name="GH_PAT")
    assert ix.followup.sent == []
    assert "Admins only" in ix.response.sent[0]

    ix = _Ix(uid=9)
    await bot.tree.commands["unsecret"](ix, name="NOPE")
    assert "No org secret" in ix.followup.sent[0]


async def test_unsecret_flat_trust_without_admins():
    """TEAM_ADMIN_IDS unset → any operator deletes (solo default)."""
    bot = _bot(admin_set=frozenset())
    ix = _Ix(uid=1)
    await bot.tree.commands["unsecret"](ix, name="GH_PAT")
    assert "Deleted" in ix.followup.sent[0]


# ---- /playbooks + /playbook-run /-save /-delete ------------------------------


async def test_playbooks_lists_titles():
    bot = _bot()
    ix = _Ix()
    await bot.tree.commands["playbooks"](ix)
    embed = ix.response.sent[0]["embed"]
    names = [f.name for f in embed.fields]
    assert "Janitor" in names and "Nightly audit" in names


async def test_playbook_run_resolves_and_spawns(tmp_path, monkeypatch):
    db = await Database.connect(str(tmp_path / "t.db"))
    bot = _bot(db)
    spawned: list[dict] = []

    async def fake_spawn(b, **kw):
        spawned.append(kw)
        return Session(session_id="s1", url="u", status="running"), \
            SimpleNamespace(mention="<#7>", id=7)

    monkeypatch.setattr(spawn_mod, "spawn_session", fake_spawn)
    monkeypatch.setattr(
        "devinmobile.bot.commands.spawn_session", fake_spawn
    )
    ix = _Ix()
    await bot.tree.commands["playbook-run"](
        ix, name="janitor", prompt="sweep the floors", repo="o/r"
    )
    assert spawned[0]["playbook_id"] == "pb-1"
    assert spawned[0]["prompt"] == "sweep the floors"
    assert spawned[0]["spawned_by"] == "1"
    assert "Janitor" in ix.followup.sent[0]


async def test_playbook_run_ambiguous_and_missing(tmp_path):
    db = await Database.connect(str(tmp_path / "t.db"))
    bot = _bot(db)
    ix = _Ix()
    await bot.tree.commands["playbook-run"](ix, name="nightly")
    assert "ambiguous" in ix.response.sent[0]
    ix = _Ix()
    await bot.tree.commands["playbook-run"](ix, name="nonexistent")
    assert "No playbook" in ix.response.sent[0]


async def test_playbook_save_create_and_replace():
    bot = _bot()
    # Modal path: send_modal captures it, then we drive on_submit
    ix = _Ix()
    await bot.tree.commands["playbook-save"](ix, name="Fresh book")
    modal = ix.response.modal
    assert modal is not None
    submit_ix = _Ix()
    await modal._cb(submit_ix, "do the fresh thing")
    assert bot.devin.created_books == [
        {"title": "Fresh book", "body": "do the fresh thing"}
    ]
    assert "Saved" in submit_ix.response.sent[0]

    # exact-title match → PUT replace (not a substring hit on another book)
    ix = _Ix()
    await bot.tree.commands["playbook-save"](ix, name="janitor")
    submit_ix = _Ix()
    await ix.response.modal._cb(submit_ix, "cleaner stuff")
    assert bot.devin.updated_books == [
        {"id": "pb-1", "title": "janitor", "body": "cleaner stuff"}
    ]
    assert "Updated" in submit_ix.response.sent[0]


async def test_playbook_delete_admin_gate():
    bot = _bot()
    ix = _Ix(uid=1)  # operator, not admin
    await bot.tree.commands["playbook-delete"](ix, name="janitor")
    assert "Admins only" in ix.response.sent[0]
    assert bot.devin.deleted_books == []

    ix = _Ix(uid=9)
    await bot.tree.commands["playbook-delete"](ix, name="janitor")
    assert "Deleted" in ix.response.sent[0]
    assert bot.devin.deleted_books == ["pb-1"]

    ix = _Ix(uid=9)
    await bot.tree.commands["playbook-delete"](ix, name="nightly")
    assert "ambiguous" in ix.response.sent[0]


# ---- spawn passthrough + the v3-only gate ------------------------------------


class _Chan:
    id = 777

    async def send(self, *a, **kw):
        return SimpleNamespace(id=900)

    async def join(self):
        pass


class _Hub:
    id = 42

    async def create_thread(self, name, type=None):
        return _Chan()


class _SpawnBot:
    def __init__(self, db, default_model=None):
        self.db = db
        self.devin = SimpleNamespace(
            created=[],
        )
        self.relay = SimpleNamespace(
            progress=SimpleNamespace(thinking=self._noop),
            request_poll=lambda b: None,
        )
        self.bridge = type("B", (), {"available": False})()
        self.settings = SimpleNamespace(
            allowed_user_id_set={1},
            hub_channel_id=42,
            hub_channel_id_map={},
            user_acu_daily=0,
            max_acu_limit=0,
            default_model=default_model,
            devin_mode="normal",
            create_as_user_id=None,
            devin_user_id_map={},
        )

    async def _noop(self, b):
        pass

    async def _create(self, **kw):
        self.devin.created.append(kw)
        return Session(session_id="s9", url="u9", status="running")

    def get_channel(self, _id):
        return _Hub()

    async def fetch_channel(self, _id):
        return _Hub()

    async def handle_component(self, *a):
        pass


async def test_spawn_passthrough_and_v3_gate(tmp_path, monkeypatch):
    """session_secrets/playbook_id reach create_session; explicit model +
    either field is a caller error (bridge can't carry them); an env
    default model silently yields rather than dropping the field."""
    db = await Database.connect(str(tmp_path / "t.db"))
    bot = _SpawnBot(db, default_model="opus")
    bot.devin.create_session = bot._create
    monkeypatch.setattr(discord, "TextChannel", _Hub)

    await spawn_mod.spawn_session(
        bot, prompt="x",
        session_secrets=[{"key": "K", "value": "v"}],
        playbook_id="pb-1",
    )
    call = bot.devin.created[0]
    assert call["session_secrets"] == [{"key": "K", "value": "v"}]
    assert call["playbook_id"] == "pb-1"
    # env default_model didn't divert to the bridge — v3 create ran

    with pytest.raises(SpawnError, match="can't combine"):
        await spawn_mod.spawn_session(
            bot, prompt="x", model="opus", playbook_id="pb-1"
        )
    with pytest.raises(SpawnError, match="can't combine"):
        await spawn_mod.spawn_session(
            bot, prompt="x", model="opus",
            session_secrets=[{"key": "K", "value": "v"}],
        )

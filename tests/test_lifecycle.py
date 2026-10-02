"""Lifecycle features: auto-respawn on error, per-session ACU budgets,
presence count, PR-review label trigger, and the Post-to-GitHub button."""

import pytest

import devinmobile.relay as relay_mod
import devinmobile.spawn as spawn_mod
from devinmobile.bot.main import DevinMobileBot
from devinmobile.db import Binding, Database
from devinmobile.models import Session
from devinmobile.relay import Relay
from devinmobile.webhook_server import WebhookServer


class _Settings:
    allowed_user_id_set = {1}
    max_acu_limit = 25
    auto_respawn = True
    github_review_label = "devin-review"
    github_webhook_secret = "wh"
    github_webhook_port = 0
    task_intake_token = ""
    task_intake_token_map: dict = {}
    silence_alert_minutes = 0
    create_as_user_id = None
    github_merge_method = "squash"


class _Chan:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text, **kw):
        self.sent.append(text)


class _Bot:
    """Just enough Client surface for Relay._thread / _presence."""

    def __init__(self):
        self.db = None
        self.settings = _Settings()
        self.presence: list[str] = []

    def get_channel(self, _id):
        return None

    async def fetch_channel(self, _id):
        return None

    def is_ready(self) -> bool:
        return True

    async def change_presence(self, *, activity=None, **kw):
        self.presence.append(activity.name if activity else "(cleared)")


def _binding(**kw) -> Binding:
    return Binding(
        session_id="s1", thread_id=10, channel_id=5, anchor_msg_id=100, **kw
    )


async def _db(tmp_path) -> Database:
    return await Database.connect(str(tmp_path / "t.db"))


# ---- db fields ------------------------------------------------------------

async def test_binding_roundtrips_lifecycle_fields(tmp_path):
    db = await _db(tmp_path)
    await db.upsert_binding(_binding(
        continued_from="s0", max_acu=3.5, review_of="o/r#9",
    ))
    b = await db.get_binding("s1")
    assert b.continued_from == "s0"
    assert b.max_acu == 3.5
    assert b.review_of == "o/r#9"
    # upsert without the fields must not wipe them
    b2 = _binding()
    await db.upsert_binding(b2)
    b3 = await db.get_binding("s1")
    assert b3.continued_from == "s0" and b3.max_acu == 3.5


async def test_binding_by_review_of_dedupes(tmp_path):
    db = await _db(tmp_path)
    assert await db.binding_by_review_of("o/r#9") is None
    await db.upsert_binding(_binding(review_of="o/r#9"))
    found = await db.binding_by_review_of("o/r#9")
    assert found and found.session_id == "s1"


# ---- budget cap -----------------------------------------------------------

async def test_acu_cap_uses_binding_budget_and_parks(tmp_path):
    db = await _db(tmp_path)
    b = _binding(max_acu=3.0)
    bot = _Bot()
    r = Relay(bot, None, db, _Settings())  # type: ignore[arg-type]
    sess = Session(
        session_id="s1", url="u", status="running", acus_consumed=3.2
    )
    await r._check_acu(b, sess)
    assert b.acu_warned & 2  # 100% bit
    assert b.active is False  # parked
    # no re-ping on the next tick
    warned = b.acu_warned
    await r._check_acu(b, sess)
    assert b.acu_warned == warned


async def test_acu_cap_falls_back_to_global(tmp_path):
    db = await _db(tmp_path)
    b = _binding()  # no max_acu
    bot = _Bot()
    r = Relay(bot, None, db, _Settings())  # type: ignore[arg-type]
    sess = Session(session_id="s1", url="u", status="running",
                   acus_consumed=26.0)  # over the global 25
    await r._check_acu(b, sess)
    assert b.acu_warned & 2 and b.active is False


# ---- presence --------------------------------------------------------------

async def test_presence_writes_once_per_count(tmp_path):
    bot = _Bot()
    r = Relay(bot, None, await _db(tmp_path), _Settings())  # type: ignore[arg-type]
    await r._presence(3)
    await r._presence(3)  # same count — no write
    await r._presence(1)
    await r._presence(0)  # idle clears the status
    assert bot.presence == [
        "3 Devin sessions", "1 Devin session", "(cleared)",
    ]


# ---- auto-respawn -----------------------------------------------------------

async def test_error_respawns_once_with_summary(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    spawned = {}

    async def fake_spawn(bot, **kw):
        spawned.update(kw)
        return Session(session_id="s2", url="u2", status="running"), _Chan()

    monkeypatch.setattr(relay_mod, "spawn_session", fake_spawn)
    bot = _Bot()
    r = Relay(bot, None, db, _Settings())  # type: ignore[arg-type]
    b = _binding(
        repos="o/r", last_msg="halfway there", url="u1", spawned_by="77",
    )
    sess = Session(
        session_id="s1", url="u1", status="error",
        status_detail="crashed",
        structured_output={
            "summary": "added half the endpoints",
            "files_changed": ["a.py", "b.py"],
        },
    )
    await r._maybe_respawn(b, sess)
    assert spawned["continued_from"] == "s1"
    assert spawned["repos"] == ["o/r"]
    # nobody initiated the respawn — ownership inherits from the parent
    assert spawned["spawned_by"] == "77"
    assert "added half the endpoints" in spawned["prompt"]
    assert "crashed" in spawned["prompt"]
    assert "a.py" in spawned["prompt"]

    # the respawned child must NOT respawn again — one-deep chains
    spawned.clear()
    child = _binding(continued_from="s1")
    await r._maybe_respawn(child, sess)
    assert spawned == {}


async def test_respawn_respects_kill_switch(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    monkeypatch.setattr(
        relay_mod, "spawn_session",
        lambda *a, **k: pytest.fail("should not spawn"),
    )
    bot = _Bot()
    s = _Settings()
    s.auto_respawn = False
    r = Relay(bot, None, db, s)  # type: ignore[arg-type]
    await r._maybe_respawn(_binding(), Session(
        session_id="s1", url="u", status="error"
    ))


# ---- review label + post-back ----------------------------------------------

def _pr_label_payload(**over):
    pr = {
        "number": 9, "title": "my change", "body": "does stuff",
        "user": {"login": "alby"},
    }
    pr.update(over.pop("pr", {}))
    return {
        "action": "labeled",
        "label": {"name": "devin-review"},
        "pull_request": pr,
        "repository": {"name": "r", "owner": {"login": "o"}},
        **over,
    }


async def test_review_label_spawns_once(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    calls = []

    async def fake_spawn(bot, **kw):
        calls.append(kw)
        # persist the review_of marker like the real spawn does
        await db.upsert_binding(Binding(
            session_id=f"sx{len(calls)}", thread_id=1, channel_id=1,
            review_of=kw["review_of"],
        ))
        return Session(session_id="sx", url="u", status="running"), _Chan()

    monkeypatch.setattr(spawn_mod, "spawn_session", fake_spawn)

    bot = _Bot()
    srv = WebhookServer(bot, db, _Settings())  # type: ignore[arg-type]
    await srv._on_pr_label(_pr_label_payload())
    await srv._on_pr_label(_pr_label_payload())  # replay — dedupe
    assert len(calls) == 1
    assert calls[0]["review_of"] == "o/r#9"
    assert calls[0]["repos"] == ["o/r"]
    assert calls[0]["spawned_by"] == "github"  # infra marker, not a user
    assert "Review pull request o/r#9" in calls[0]["prompt"]


async def test_review_label_ignores_bot_prs_and_other_labels(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    monkeypatch.setattr(
        spawn_mod, "spawn_session",
        lambda *a, **k: pytest.fail("should not spawn"),
    )
    srv = WebhookServer(_Bot(), db, _Settings())  # type: ignore[arg-type]
    await srv._on_pr_label(_pr_label_payload(
        pr={"user": {"login": "devin-ai-integration[bot]"}}))
    await srv._on_pr_label(_pr_label_payload(label={"name": "bug"}))


# ---- post_review button -----------------------------------------------------

class _Resp:
    def __init__(self):
        self.done = False
        self.sent: list[str] = []

    def is_done(self):
        return self.done

    async def send_message(self, text, **kw):
        self.sent.append(text)
        self.done = True

    async def defer(self, **kw):
        self.done = True


class _Followup:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text, **kw):
        self.sent.append(text)


class _Ix:
    def __init__(self, uid=1):
        self.user = type("U", (), {"id": uid})()
        self.response = _Resp()
        self.followup = _Followup()
        self.message = None


async def test_post_review_posts_last_findings(tmp_path):
    db = await _db(tmp_path)
    await db.upsert_binding(_binding(
        review_of="o/r#9", last_msg="1. bug in a.py:12 …",
    ))

    posted = {}

    class _Gh:
        async def create_pr_review(self, ref, body):
            posted["ref"] = ref
            posted["body"] = body

    class _Dev:
        async def get_session(self, sid):
            return Session(
                session_id=sid, url="https://devin/s1", status="exit",
                structured_output={"summary": "found a bug"},
            )

    bot = _Bot()
    bot.db = db
    bot.github = _Gh()
    bot.devin = _Dev()
    ix = _Ix()
    await DevinMobileBot.handle_component(
        bot, ix, "post_review", "s1", "o/r#9"  # type: ignore[arg-type]
    )
    assert posted["ref"].owner == "o" and posted["ref"].number == 9
    assert "bug in a.py:12" in posted["body"]
    assert "https://devin/s1" in posted["body"]
    assert "comment on o/r#9" in ix.followup.sent[-1]


async def test_post_review_rejects_bad_ref(tmp_path):
    db = await _db(tmp_path)
    await db.upsert_binding(_binding(review_of="o/r#9"))
    bot = _Bot()
    bot.db = db
    bot.github = object()
    ix = _Ix()
    await DevinMobileBot.handle_component(
        bot, ix, "post_review", "s1", "nonsense"  # type: ignore[arg-type]
    )
    assert "Bad PR reference" in ix.response.sent[-1]

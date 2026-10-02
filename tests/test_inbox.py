"""Inbox card — build_inbox sections + build_inbox_embed's gather
(inbox_bindings filter, continued-parent dedup, red monitors, open PRs,
idle-repo suggestions)."""

import time

import discord

from devinmobile.db import Binding, Database, PrRow, ScheduleRow
from devinmobile.inbox import build_inbox, build_inbox_embed
from devinmobile.scheduler import Scheduler


async def _db(tmp_path) -> Database:
    return await Database.connect(str(tmp_path / "t.db"))


def _b(sid, **kw) -> Binding:
    kw.setdefault("thread_id", 1)
    kw.setdefault("channel_id", 5)
    return Binding(session_id=sid, **kw)


# ---- build_inbox (pure) ------------------------------------------------------


def test_build_inbox_sections():
    embed = build_inbox(
        waiting=[
            _b("w1", active=True, status="blocked",
               status_detail="waiting_for_user",
               title="Auth rework", last_msg="Ship it?"),
            _b("w2", active=True, status="blocked",
               status_detail="waiting_for_approval", title="Deploy"),
        ],
        errored=[_b("e1", status="error", title="Flaky")],
        pending_chains=[
            _b("c1", status="exit", title="janitor p1",
               chain={"pending": 1}),
        ],
        red_monitors=[ScheduleRow(id=3, prompt="x", watch="https://h/health",
                                  watch_state="red", kind="monitor")],
        open_prs=[
            PrRow(session_id="s", pr_url="u", owner="o", repo="r",
                  number=7, state="open", checks_state="pending",
                  auto_merge=True),
        ],
        running_n=3,
    )
    names = [f.name for f in embed.fields]
    assert names == [
        "Waiting on you (2)", "Errored (1)", "Chains awaiting Continue → (1)",
        "Monitors red (1)", "Open PRs (1)",
    ]
    # question excerpt rides the waiting row
    assert "Ship it?" in embed.fields[0].value
    assert "armed" in embed.fields[4].value
    assert "6 waiting · 3 running" in (embed.footer.text or "")


def test_build_inbox_zero_with_suggestions():
    embed = build_inbox([], [], [], [], [], 0,
                        suggestions=["o/idler", "o/stale"])
    assert "Inbox zero" in (embed.description or "")
    assert "`o/idler`" in (embed.description or "")
    embed = build_inbox([], [], [], [], [], 0)
    assert "Idle" not in (embed.description or "")


# ---- build_inbox_embed (db gather) --------------------------------------------


async def test_inbox_gather_filters(tmp_path):
    db = await _db(tmp_path)
    now = int(time.time())
    await db.upsert_binding(_b(
        "wait1", active=True, status="blocked",
        status_detail="waiting_for_user", title="T", created_at=now,
    ))
    await db.upsert_binding(_b(
        "err1", status="error", active=False, created_at=now,
    ))
    # errored-with-child is not inbox work — the continuation owns it
    await db.upsert_binding(_b(
        "err-old", status="error", active=False, created_at=now - 100,
    ))
    await db.upsert_binding(_b(
        "err-child", status="running", active=True,
        continued_from="err-old", created_at=now,
    ))
    await db.upsert_binding(_b(
        "chain1", status="exit", active=False, created_at=now,
        chain={"pending": 1, "playbook": "janitor"},
    ))
    # exited but still polling for an armed auto-merge — a zombie, not
    # "running" for the footer count
    await db.upsert_binding(_b(
        "zombie", status="exit", active=True, created_at=now,
    ))
    await db.add_schedule(ScheduleRow(
        id=0, prompt="x", kind="monitor", watch="https://h",
        watch_state="red",
    ))
    await db.upsert_pr(PrRow(
        session_id="s", pr_url="u", owner="o", repo="r", number=1,
        state="open", checks_state="success",
    ))
    embed = await build_inbox_embed(db)
    names = [f.name for f in embed.fields]
    assert names == [
        "Waiting on you (1)", "Errored (1)", "Chains awaiting Continue → (1)",
        "Monitors red (1)", "Open PRs (1)",
    ]
    # err-old suppressed by its continuation child; running child isn't
    # "waiting", just counted — and the armed-PR zombie isn't "running"
    assert "Errored (1)" in names and "Errored (2)" not in names
    assert "2 running" in (embed.footer.text or "")


async def test_inbox_idle_suggestions(tmp_path):
    db = await _db(tmp_path)
    now = int(time.time())
    await db.add_note("o/quiet", "x", "u1")
    await db.upsert_pr(PrRow(
        session_id="s", pr_url="u", owner="o", repo="merged", number=1,
        state="merged",
    ))
    # busy repo — had a session in the window — shouldn't suggest
    await db.upsert_binding(_b(
        "act", status="exit", repos="o/busy", last_activity_at=now,
        created_at=now,
    ))
    embed = await build_inbox_embed(db)
    assert "Inbox zero" in (embed.description or "")
    assert "`o/quiet`" in (embed.description or "")
    assert "`o/merged`" in (embed.description or "")
    assert "`o/busy`" not in (embed.description or "")


# ---- kind=inbox schedule -----------------------------------------------------


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
    def __init__(self, db):
        self.db = db
        self.settings = _Settings()
        self.hub = _Chan()

    def get_channel(self, _id):
        return self.hub

    async def fetch_channel(self, _id):
        return self.hub


async def test_inbox_schedule_posts_card(tmp_path):
    db = await _db(tmp_path)
    now = int(time.time())
    await db.upsert_binding(_b(
        "w", active=True, status="blocked",
        status_detail="waiting_for_approval", title="W",
    ))
    row = ScheduleRow(
        id=0, prompt="inbox", interval_seconds=86400,
        next_run_at=now - 10, kind="inbox",
    )
    row.id = await db.add_schedule(row)
    bot = _Bot(db)
    await Scheduler(bot)._fire_due()  # type: ignore[arg-type]
    embeds = [kw["embed"] for _, kw in bot.hub.sent if "embed" in kw]
    assert embeds and "inbox" in embeds[0].title.lower()
    assert "Waiting on you" in embeds[0].fields[0].name
    assert (await db.all_schedules())[0].next_run_at > now

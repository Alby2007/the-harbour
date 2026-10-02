"""Command-layer pure helpers: usage_summary bucketing + repo attribution."""

import time

from devinmobile.bot.commands import parse_every, usage_summary
from devinmobile.db import Binding


def _b(sid, acus, created_at, repos="", title=None, spawned_by=""):
    return Binding(
        session_id=sid, thread_id=1, channel_id=1,
        acus=acus, created_at=created_at, repos=repos, title=title,
        spawned_by=spawned_by,
    )


def test_parse_every():
    assert parse_every("30m") == 1800
    assert parse_every("6H") == 21600
    assert parse_every("1d") == 86400
    assert parse_every("bogus") is None
    assert parse_every("") is None


def test_usage_summary_buckets_and_repos():
    now = time.time()
    day = 24 * 3600
    bs = [
        _b("a", 3.0, now - 100, repos="o/x"),                       # today
        _b("b", 2.0, now - 3 * day, repos="o/x"),                   # this week
        _b("c", 10.0, now - 30 * day, repos="o/y", title="old big"),# old
        _b("d", 1.0, now - 100),                                    # no repo
    ]
    s = usage_summary(bs, now)
    assert s["today"] == 4.0
    assert s["week"] == 6.0
    assert s["total"] == 16.0
    assert s["count"] == 4
    assert s["top"][0].session_id == "c"
    assert s["by_repo"]["o/x"] == [5.0, 2]
    assert s["by_repo"]["o/y"] == [10.0, 1]
    assert s["by_repo"]["(no repo)"] == [1.0, 1]
    assert s["by_user"]["unattributed"] == [16.0, 4]


def test_usage_summary_by_user():
    now = time.time()
    bs = [
        _b("a", 3.0, now - 100, spawned_by="42"),
        _b("b", 2.0, now - 100, spawned_by="42"),
        _b("c", 10.0, now - 100, spawned_by="github"),
        _b("d", 1.0, now - 100),  # legacy row — no attribution
    ]
    s = usage_summary(bs, now)
    assert s["by_user"]["42"] == [5.0, 2]
    assert s["by_user"]["github"] == [10.0, 1]
    assert s["by_user"]["unattributed"] == [1.0, 1]


def test_usage_summary_empty():
    s = usage_summary([], time.time())
    assert s["total"] == 0 and s["count"] == 0
    assert s["top"] == [] and s["by_repo"] == {}

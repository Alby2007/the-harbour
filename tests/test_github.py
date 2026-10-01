"""Tests for the GitHub integration surface: client parsing/HTTP paths,
prs table, PR custom-ids, webhook receiver, and relay PR tracking."""

from __future__ import annotations

import hashlib
import hmac
import time
from types import SimpleNamespace
from typing import Any

import discord
import httpx
import pytest

from devinmobile.config import Settings
from devinmobile.db import Binding, Database, PrRow
from devinmobile.github_client import (
    GithubClient,
    GithubError,
    PullRef,
    checks_state,
    parse_issue_ref,
    parse_pr_url,
)
from devinmobile.models import PullRequest, Session
from devinmobile.relay import Relay
from devinmobile.views import make_custom_id, parse_custom_id
from devinmobile.webhook_server import WebhookServer


def _settings(**kw: Any) -> Settings:
    # _env_file=None keeps the repo's real .env out of tests
    return Settings(_env_file=None, allowed_user_ids="111", **kw)


# ---- pure helpers -----------------------------------------------------------


def test_parse_pr_url():
    ref = parse_pr_url("https://github.com/Alby2007/Stockbot/pull/42")
    assert ref == PullRef(owner="Alby2007", repo="Stockbot", number=42)
    assert ref.key == "Alby2007/Stockbot#42"
    assert ref.url == "https://github.com/Alby2007/Stockbot/pull/42"
    assert parse_pr_url("https://www.github.com/o/r/pulls/7").number == 7
    assert parse_pr_url("see https://github.com/o/r/pull/3 trailing").number == 3
    assert parse_pr_url("https://gitlab.com/o/r/pull/3") is None
    assert parse_pr_url("not a url") is None


def test_parse_issue_ref():
    assert parse_issue_ref(
        "https://github.com/Alby2007/Stockbot/issues/9"
    ) == ("Alby2007", "Stockbot", 9)
    assert parse_issue_ref("Alby2007/Stockbot#9") == ("Alby2007", "Stockbot", 9)
    assert parse_issue_ref("#9", default_repo="Alby2007/Stockbot") == (
        "Alby2007", "Stockbot", 9,
    )
    # bare #n with no repo context can't resolve
    assert parse_issue_ref("#9") is None
    assert parse_issue_ref("garbage") is None


def test_checks_state_rollup():
    def run(status="completed", conclusion=None):
        return {"status": status, "conclusion": conclusion}
    assert checks_state([]) == "none"
    assert checks_state([run(conclusion="success")]) == "success"
    assert checks_state([run(conclusion="skipped"), run(conclusion="neutral")]) == "success"
    assert checks_state([run(status="in_progress")]) == "pending"
    # failure beats pending — a red run is decisive even while others run
    assert checks_state(
        [run(status="in_progress"), run(conclusion="failure")]
    ) == "failure"
    assert checks_state([run(conclusion="timed_out")]) == "failure"


def test_pr_custom_id_roundtrip():
    cid = make_custom_id("pr_merge", "devin-abc", "42")
    assert parse_custom_id(cid) == ("pr_merge", "devin-abc", "42")
    cid = make_custom_id("refresh", "devin-abc")
    assert parse_custom_id(cid) == ("refresh", "devin-abc", None)
    # foreign/legacy ids are ignored
    assert parse_custom_id("other:pr_merge:x") is None
    assert parse_custom_id("dvm:notanaction:x") is None


# ---- GithubClient -----------------------------------------------------------


def _gh_client(handler) -> GithubClient:
    client = GithubClient(
        "123", "/nonexistent.pem", "999",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ),
    )
    # Pre-seed a valid installation token so tests never touch JWT/PEM code.
    client._token = "ghs_test"
    client._token_exp = time.time() + 3000
    return client


async def test_client_request_paths():
    seen: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.read() or None))
        path = request.url.path
        if path.endswith("/merge"):
            return httpx.Response(200, json={"merged": True, "sha": "abc"})
        if path.endswith("/reviews"):
            return httpx.Response(200, json={"id": 1, "state": "APPROVED"})
        if path.endswith("/check-runs"):
            return httpx.Response(
                200, json={"check_runs": [{"status": "completed", "conclusion": "success"}]}
            )
        if request.method == "PATCH":
            return httpx.Response(200, json={"state": "closed"})
        return httpx.Response(200, json={"title": "t", "head": {"sha": "deadbeef"}})

    gh = _gh_client(handler)
    ref = PullRef("o", "r", 5)
    assert (await gh.get_pr(ref))["title"] == "t"
    assert (await gh.merge_pr(ref, method="squash"))["merged"] is True
    assert (await gh.approve_pr(ref))["state"] == "APPROVED"
    assert (await gh.close_pr(ref))["state"] == "closed"
    assert await gh.get_checks(ref) == "success"
    await gh.aclose()

    methods = [m for m, _, _ in seen]
    assert methods == ["GET", "PUT", "POST", "PATCH", "GET", "GET"]
    assert seen[1][0] == "PUT" and b'"squash"' in seen[1][2]
    assert seen[2][1] == "/repos/o/r/pulls/5/reviews"


async def test_client_error_surfaces_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    gh = _gh_client(handler)
    with pytest.raises(GithubError, match="Not Found"):
        await gh.get_pr(PullRef("o", "r", 1))
    await gh.aclose()


# ---- prs table ----------------------------------------------------------------


async def test_pr_roundtrip_and_join(tmp_path):
    db = await Database.connect(str(tmp_path / "t.db"))
    await db.upsert_binding(
        Binding(session_id="s1", thread_id=10, channel_id=1)
    )
    url = "https://github.com/o/r/pull/5"
    await db.upsert_pr(
        PrRow(session_id="s1", pr_url=url, owner="o", repo="r", number=5,
              state="open", card_msg_id=777)
    )
    row = await db.get_pr("s1", url)
    assert row and row.number == 5 and row.card_msg_id == 777

    # COALESCE upsert: a state-only update must not wipe card_msg_id
    await db.upsert_pr(
        PrRow(session_id="s1", pr_url=url, state="merged",
              checks_state="success")
    )
    row = await db.get_pr("s1", url)
    assert row.state == "merged" and row.card_msg_id == 777

    assert (await db.get_pr_by_number("s1", 5)).pr_url == url
    assert [p.number for p in await db.prs_for_session("s1")] == [5]

    found = await db.binding_for_pr("o", "r", 5)
    assert found is not None
    binding, pr = found
    assert binding.session_id == "s1" and binding.thread_id == 10
    assert pr.pr_url == url
    assert await db.binding_for_pr("o", "r", 999) is None
    await db.close()


# ---- webhook receiver ---------------------------------------------------------


class _FakeThread(discord.abc.Messageable):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def _get_channel(self):
        return self

    async def send(self, text: str) -> None:
        self.sent.append(text)


class _FakeBot:
    def __init__(self, thread) -> None:
        self._thread = thread
        self.settings = _settings()

    def get_channel(self, _id):
        return self._thread


def _server(bot, db) -> WebhookServer:
    return WebhookServer(bot, db, _settings(github_webhook_secret="whsec"))


async def test_webhook_signature():
    srv = _server(_FakeBot(_FakeThread()), db=None)
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert srv._verify(body, sig)
    assert not srv._verify(body, "sha256=deadbeef")
    assert not srv._verify(body, None)
    assert not srv._verify(b'tampered', sig)


async def test_webhook_pull_request_event(tmp_path):
    db = await Database.connect(str(tmp_path / "t.db"))
    await db.upsert_binding(Binding(session_id="s1", thread_id=10, channel_id=1))
    await db.upsert_pr(PrRow(
        session_id="s1", pr_url="https://github.com/o/r/pull/5",
        owner="o", repo="r", number=5, state="open",
    ))
    thread = _FakeThread()
    srv = _server(_FakeBot(thread), db)
    await srv._on_pull_request({
        "repository": {"name": "r", "owner": {"login": "o"}},
        "pull_request": {"number": 5, "merged": True, "state": "closed",
                         "title": "My PR"},
    })
    row = await db.get_pr_by_number("s1", 5)
    assert row.state == "merged" and row.pr_title == "My PR"
    assert any("merged" in m for m in thread.sent)

    # unknown PR — silently ignored
    await srv._on_pull_request({
        "repository": {"name": "r", "owner": {"login": "o"}},
        "pull_request": {"number": 999, "state": "open"},
    })
    assert await db.get_pr_by_number("s1", 999) is None
    await db.close()


# ---- relay PR tracking --------------------------------------------------------


class _Card:
    def __init__(self, mid: int) -> None:
        self.id = mid
        self.edits: list[Any] = []

    async def edit(self, **kw: Any) -> None:
        self.edits.append(kw)


class _RelayThread:
    """Stands in for discord.Thread — records sends and card edits."""

    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.cards: dict[int, _Card] = {}
        self._next = 1000

    async def send(self, *args: Any, **kw: Any) -> Any:
        self.sent.append((args, kw))
        if "embed" in kw:
            card = _Card(self._next)
            self.cards[self._next] = card
            self._next += 1
            return card
        return SimpleNamespace(id=self._next)

    async def fetch_message(self, mid: int) -> _Card:
        return self.cards[mid]


class _FakeGithub:
    def __init__(self, pr_data: dict, checks: str = "none") -> None:
        self.pr_data = pr_data
        self.checks = checks

    async def get_pr(self, ref: PullRef) -> dict:
        return self.pr_data

    async def get_checks(self, ref: PullRef) -> str:
        return self.checks


async def _relay(db, thread, github) -> Relay:
    r = Relay(
        bot=SimpleNamespace(get_channel=lambda _id: thread),
        devin=None,
        db=db,
        settings=_settings(),
        github=github,
        component_handler=None,
    )
    async def _t(_b):  # bypass isinstance(discord.Thread)
        return thread
    r._thread = _t
    return r


def _session_with_pr(url: str, state: str = "open") -> Session:
    return Session(
        session_id="s1", url="https://x", status="running",
        pull_requests=[PullRequest(pr_url=url, pr_state=state)],
    )


async def test_sync_prs_posts_card_once(tmp_path):
    db = await Database.connect(str(tmp_path / "t.db"))
    binding = Binding(session_id="s1", thread_id=10, channel_id=1)
    await db.upsert_binding(binding)
    url = "https://github.com/o/r/pull/5"
    thread = _RelayThread()
    gh = _FakeGithub({"title": "Fix stuff", "state": "open", "merged": False},
                     checks="pending")
    relay = await _relay(db, thread, gh)

    sess = _session_with_pr(url)
    await relay._sync_prs(binding, sess)
    assert (await db.get_pr("s1", url)).pr_title == "Fix stuff"

    n_cards = len(thread.cards)
    await relay._sync_prs(binding, sess)
    assert len(thread.cards) == n_cards  # second sync must not repost
    await db.close()


async def test_poll_pr_notifies_on_merge(tmp_path):
    db = await Database.connect(str(tmp_path / "t.db"))
    binding = Binding(session_id="s1", thread_id=10, channel_id=1)
    await db.upsert_binding(binding)
    url = "https://github.com/o/r/pull/5"
    thread = _RelayThread()
    gh = _FakeGithub({"title": "t", "state": "open", "merged": False})
    relay = await _relay(db, thread, gh)

    await relay._sync_prs(binding, _session_with_pr(url))
    baseline = len(thread.sent)

    gh.pr_data = {"title": "t", "state": "closed", "merged": True}
    await relay._sync_prs(binding, _session_with_pr(url))
    assert any("merged" in str(args[0]) for args, _ in thread.sent[baseline:])
    row = await db.get_pr("s1", url)
    assert row.state == "merged"

    # second poll of the same transition -> no duplicate post
    again = len(thread.sent)
    await relay._sync_prs(binding, _session_with_pr(url))
    assert len(thread.sent) == again
    await db.close()


async def test_poll_pr_ci_failure_mentions(tmp_path):
    db = await Database.connect(str(tmp_path / "t.db"))
    binding = Binding(session_id="s1", thread_id=10, channel_id=1)
    await db.upsert_binding(binding)
    url = "https://github.com/o/r/pull/5"
    thread = _RelayThread()
    gh = _FakeGithub({"title": "t", "state": "open", "merged": False})
    relay = await _relay(db, thread, gh)

    await relay._sync_prs(binding, _session_with_pr(url))
    gh.checks = "failure"
    await relay._sync_prs(binding, _session_with_pr(url))
    texts = [str(a[0]) for a, _ in thread.sent if a]
    assert any("<@111>" in t and "CI failing" in t for t in texts)
    await db.close()

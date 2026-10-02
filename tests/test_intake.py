"""POST /task intake — bearer-authed spawn over HTTP so non-Discord
clients (Siri, Raycast, scripts) can create sessions."""

import aiohttp

import devinmobile.spawn as spawn_mod
from devinmobile.models import Session
from devinmobile.webhook_server import WebhookServer, maybe_start


class _Settings:
    github_webhook_secret = ""
    github_webhook_port = 0  # ephemeral — the OS picks the port
    task_intake_token = "tok123"
    github_enabled = False


class _Guild:
    id = 555


class _Thread:
    id = 4242
    guild = _Guild()

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text, **kw):
        self.sent.append(text)
        return None


class _Bot:
    def __init__(self, settings):
        self.settings = settings


async def _server(monkeypatch, settings=None, spawned=None):
    """Start a real server on PORT with spawn_session stubbed."""
    calls: list[dict] = []

    async def fake_spawn(bot, **kw):
        calls.append(kw)
        t = _Thread()
        if spawned is not None:
            spawned.append(t)
        return Session(
            session_id="s-intake", url="https://devin.ai/s", status="running"
        ), t

    monkeypatch.setattr(spawn_mod, "spawn_session", fake_spawn)
    srv = WebhookServer(_Bot(settings or _Settings()), None, settings or _Settings())  # type: ignore[arg-type]
    await srv.start()
    return srv, calls


def _port(srv: WebhookServer) -> int:
    return srv._runner.addresses[0][1]  # ephemeral port actually bound


async def _post(port, path, token=None, json=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with aiohttp.ClientSession() as cs:
        async with cs.post(
            f"http://127.0.0.1:{port}{path}", headers=headers, json=json
        ) as resp:
            body = (
                await resp.json()
                if "application/json" in resp.headers.get("Content-Type", "")
                else {}
            )
            return resp.status, body


async def test_task_intake_happy(monkeypatch):
    srv, calls = await _server(monkeypatch)
    try:
        port = _port(srv)
        status, body = await _post(port, "/task", token="tok123", json={
            "prompt": "fix the flake", "repo": "o/r, o/r2",
            "title": "Flake fix", "budget": 5,
        })
        assert status == 200
        assert body["session_id"] == "s-intake"
        assert body["session_url"] == "https://devin.ai/s"
        assert body["thread_url"] == (
            "https://discord.com/channels/555/4242"
        )
        assert calls[0]["prompt"] == "fix the flake"
        assert calls[0]["repos"] == ["o/r", "o/r2"]
        assert calls[0]["title"] == "Flake fix"
        assert calls[0]["budget"] == 5.0
        # repo as an array — entries are stripped like the comma form
        status, _ = await _post(port, "/task", token="tok123", json={
            "prompt": "again", "repo": [" o/r ", "o/r2"],
        })
        assert status == 200
        assert calls[1]["repos"] == ["o/r", "o/r2"]
        # attachments forward as attachment_urls; absent → None
        status, _ = await _post(port, "/task", token="tok123", json={
            "prompt": "look", "attachments": ["https://cdn/x.png"],
        })
        assert status == 200
        assert calls[2]["attachment_urls"] == ["https://cdn/x.png"]
        assert calls[0]["attachment_urls"] is None
    finally:
        await srv.stop()


async def test_task_auth_and_validation(monkeypatch):
    srv, _ = await _server(monkeypatch)
    try:
        port = _port(srv)
        status, _ = await _post(port, "/task", json={"prompt": "x"})
        assert status == 401
        status, _ = await _post(port, "/task", token="wrong",
                                json={"prompt": "x"})
        assert status == 401
        # non-ASCII bearer → 401, not a 500 from str compare_digest
        status, _ = await _post(port, "/task", token="tök",
                                json={"prompt": "x"})
        assert status == 401
        status, _ = await _post(port, "/task", token="tok123",
                                json={"nope": 1})
        assert status == 400
        status, _ = await _post(
            port, "/task", token="tok123", json={"prompt": "x" * 4001}
        )
        assert status == 400
        status, _ = await _post(port, "/task", token="tok123",
                                json={"prompt": ""})
        assert status == 400
        # present-but-wrong-type optional fields reject instead of
        # silently spawning a different task than the caller asked for
        for bad in (
            {"prompt": "x", "repo": 123},
            {"prompt": "x", "repo": ["ok", 1]},
            {"prompt": "x", "title": 7},
            {"prompt": "x", "budget": "5"},
            {"prompt": "x", "budget": True},  # bool is an int → 1 ACU
            {"prompt": "x", "budget": -1},
            {"prompt": "x", "budget": float("nan")},
            {"prompt": "x", "budget": float("inf")},
            {"prompt": "x", "budget": 10**400},  # float() overflows
            # attachments: wrong type / >8 / non-http scheme / oversized
            {"prompt": "x", "attachments": "https://cdn/x.png"},
            {"prompt": "x", "attachments": ["https://x"] * 9},
            {"prompt": "x", "attachments": ["ftp://x", "https://y"]},
            {"prompt": "x", "attachments": ["https://" + "u" * 2050]},
        ):
            status, _ = await _post(port, "/task", token="tok123", json=bad)
            assert status == 400, bad
    finally:
        await srv.stop()


async def test_task_route_absent_without_token(monkeypatch):
    class _NoToken(_Settings):
        task_intake_token = ""

    srv, _ = await _server(monkeypatch, settings=_NoToken())
    try:
        status, _ = await _post(
            _port(srv), "/task", token="tok123", json={"prompt": "x"}
        )
        assert status == 404
    finally:
        await srv.stop()


async def test_maybe_start_token_only(tmp_path):
    """The server starts on the intake token alone — github not required."""
    srv = await maybe_start(_Bot(_Settings()), None, _Settings())  # type: ignore[arg-type]
    assert srv is not None
    await srv.stop()
    # nothing configured at all → no listener
    class _Off(_Settings):
        task_intake_token = ""

    assert await maybe_start(_Bot(_Off()), None, _Off()) is None  # type: ignore[arg-type]

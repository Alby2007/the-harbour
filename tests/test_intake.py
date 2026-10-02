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
    task_intake_token_map = {"mapA": "alby", "mapB": "sam"}
    github_user_id_map: dict = {}
    allowed_user_id_set = {1}
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


class _Devin:
    """Secret ops for the /secret route tests."""

    def __init__(self):
        self.secrets: list[dict] = [
            {"secret_id": "sec-1", "key": "EXISTING",
             "secret_type": "key-value"},
        ]
        self.created: list[dict] = []

    async def list_secrets(self):
        return list(self.secrets)

    async def create_secret(self, key, value, *, type="key-value", note=None):
        self.created.append(
            {"key": key, "value": value, "type": type, "note": note}
        )
        return {"secret_id": "sec-new"}

    async def delete_secret_by_key(self, key):
        for s in self.secrets:
            if s["key"] == key:
                self.secrets.remove(s)
                return True
        return False


class _Bot:
    def __init__(self, settings):
        self.settings = settings
        self.devin = _Devin()


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
        task_intake_token_map = {}

    srv, _ = await _server(monkeypatch, settings=_NoToken())
    try:
        status, _ = await _post(
            _port(srv), "/task", token="tok123", json={"prompt": "x"}
        )
        assert status == 404
    finally:
        await srv.stop()


async def test_task_token_map_attribution(monkeypatch):
    """A mapped token attributes the spawn to its name — and ignores a
    `by:` field (the mapping is the stronger claim)."""
    srv, calls = await _server(monkeypatch)
    try:
        status, _ = await _post(port := _port(srv), "/task", token="mapA",
                                json={"prompt": "x", "by": "mallory"})
        assert status == 200
        assert calls[0]["spawned_by"] == "alby"
        status, _ = await _post(port, "/task", token="mapB",
                                json={"prompt": "x"})
        assert status == 200
        assert calls[1]["spawned_by"] == "sam"
        # unknown token — neither map key nor single token → 401
        status, _ = await _post(port, "/task", token="nope",
                                json={"prompt": "x"})
        assert status == 401
    finally:
        await srv.stop()


async def test_task_by_field_under_single_token(monkeypatch):
    """Unmapped auth (the bare TASK_INTAKE_TOKEN) takes `by:` as caller
    identity, defaulting to the 'intake' marker."""
    srv, calls = await _server(monkeypatch)
    try:
        port = _port(srv)
        status, _ = await _post(port, "/task", token="tok123",
                                json={"prompt": "x"})
        assert status == 200
        assert calls[0]["spawned_by"] == "intake"
        status, _ = await _post(port, "/task", token="tok123",
                                json={"prompt": "x", "by": "siri"})
        assert status == 200
        assert calls[1]["spawned_by"] == "siri"
        for bad in (7, "", "a" * 65, ["x"]):
            status, _ = await _post(
                port, "/task", token="tok123",
                json={"prompt": "x", "by": bad},
            )
            assert status == 400, bad
    finally:
        await srv.stop()


async def test_task_by_snowflake_needs_allowlist(monkeypatch):
    """A digit `by:` claims a Discord identity — only allowlisted ids pass;
    junk digits would mint fresh ACU quota buckets / charge someone else's."""
    srv, calls = await _server(monkeypatch)
    try:
        port = _port(srv)
        # allowlisted snowflake — the documented Shortcut-owner flow
        status, _ = await _post(port, "/task", token="tok123",
                                json={"prompt": "x", "by": "1"})
        assert status == 200
        assert calls[0]["spawned_by"] == "1"
        # a digit that isn't an allowlisted id → 400, not attribution
        status, body = await _post(port, "/task", token="tok123",
                                   json={"prompt": "x", "by": "999"})
        assert status == 400
        assert "allowlisted" in body.get("error", "")
        assert len(calls) == 1  # nothing spawned
    finally:
        await srv.stop()


async def test_task_secrets_passthrough(monkeypatch):
    """`secrets: {K: V}` becomes session_secrets [{key,value}] — literal
    per-session env injection, session-scoped (unlike org secrets)."""
    srv, calls = await _server(monkeypatch)
    try:
        port = _port(srv)
        status, _ = await _post(port, "/task", token="tok123", json={
            "prompt": "deploy it",
            "secrets": {"GH_PAT": "s3cr3t", "NPM": "tok"},
        })
        assert status == 200
        assert calls[0]["session_secrets"] == [
            {"key": "GH_PAT", "value": "s3cr3t"},
            {"key": "NPM", "value": "tok"},
        ]
        # absent and {} both mean "no secrets field"
        status, _ = await _post(port, "/task", token="tok123",
                                json={"prompt": "x"})
        assert status == 200
        assert calls[1]["session_secrets"] is None
        status, _ = await _post(port, "/task", token="tok123",
                                json={"prompt": "x", "secrets": {}})
        assert status == 200
        assert calls[2]["session_secrets"] is None
        # wrong shapes → 400, not a silently-dropped injection
        for bad in (
            "GH_PAT=x",                      # not a dict
            [{"key": "K", "value": "v"}],    # list form
            {"K": 7},                        # non-str value
            {"": "v"},                       # empty key
            {"K" * 129: "v"},                # oversized key
            {f"K{i}": "v" for i in range(17)},  # >16 entries
        ):
            status, _ = await _post(port, "/task", token="tok123",
                                    json={"prompt": "x", "secrets": bad})
            assert status == 400, bad
    finally:
        await srv.stop()


# ---- POST/DELETE /secret — org-secret intake -----------------------------


async def _del(port, path, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with aiohttp.ClientSession() as cs:
        async with cs.delete(
            f"http://127.0.0.1:{port}{path}", headers=headers
        ) as resp:
            body = (
                await resp.json()
                if "application/json" in resp.headers.get("Content-Type", "")
                else {}
            )
            return resp.status, body


async def test_secret_intake_happy_and_dup(monkeypatch):
    srv, _ = await _server(monkeypatch)
    try:
        port = _port(srv)
        status, body = await _post(port, "/secret", token="tok123", json={
            "key": "NPM_TOKEN", "value": "npm_xyz", "type": "key-value",
            "note": "publish",
        })
        assert status == 200
        assert body["key"] == "NPM_TOKEN"
        assert "value" not in body  # never echoed
        assert "auto-inject" in body["scope"]
        created = srv.bot.devin.created[0]
        assert created["key"] == "NPM_TOKEN"
        assert created["value"] == "npm_xyz"
        # duplicate key → 409 (list-first guard), not a silent overwrite
        status, body = await _post(port, "/secret", token="tok123", json={
            "key": "EXISTING", "value": "v",
        })
        assert status == 409
        # a mapped intake token authenticates too
        status, _ = await _post(port, "/secret", token="mapA", json={
            "key": "K2", "value": "v",
        })
        assert status == 200
    finally:
        await srv.stop()


async def test_secret_intake_auth_and_validation(monkeypatch):
    srv, _ = await _server(monkeypatch)
    try:
        port = _port(srv)
        status, _ = await _post(port, "/secret",
                                json={"key": "K", "value": "v"})
        assert status == 401
        status, _ = await _post(port, "/secret", token="wrong",
                                json={"key": "K", "value": "v"})
        assert status == 401
        for bad in (
            {"value": "v"},                    # no key
            {"key": "K"},                      # no value
            {"key": "", "value": "v"},
            {"key": "K", "value": ""},
            {"key": "K", "value": "v", "type": "bogus"},
            {"key": "K", "value": "v", "note": 7},
            "not a dict",
        ):
            status, _ = await _post(port, "/secret", token="tok123",
                                    json=bad)
            assert status == 400, bad
        assert srv.bot.devin.created == []  # nothing was created
    finally:
        await srv.stop()


async def test_secret_delete_route(monkeypatch):
    srv, _ = await _server(monkeypatch)
    try:
        port = _port(srv)
        status, body = await _del(port, "/secret/EXISTING", token="tok123")
        assert status == 200 and body["deleted"] is True
        status, _ = await _del(port, "/secret/EXISTING", token="tok123")
        assert status == 404  # already gone
        status, _ = await _del(port, "/secret/NOPE", token="tok123")
        assert status == 404
        status, _ = await _del(port, "/secret/NOPE", token="wrong")
        assert status == 401
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
        task_intake_token_map = {}

    assert await maybe_start(_Bot(_Off()), None, _Off()) is None  # type: ignore[arg-type]

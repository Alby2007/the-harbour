import json

import httpx
import pytest

from devinmobile.devin_client import DevinClient

SESSION_JSON = {
    "session_id": "devin-abc",
    "url": "https://app.devin.ai/sessions/devin-abc",
    "status": "running",
    "status_detail": "working",
    "tags": [],
    "org_id": "org-x",
    "created_at": 1,
    "updated_at": 2,
    "acus_consumed": 1.5,
    "pull_requests": [{"pr_url": "https://github.com/o/r/pull/1", "pr_state": "open"}],
    "title": "fix thing",
    "devin_mode": "lite",
}


def make_client(handler) -> DevinClient:
    return DevinClient("cog_test", "org-x", transport=httpx.MockTransport(handler))


async def test_auth_header_and_org_path():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        return httpx.Response(200, json=SESSION_JSON)

    client = make_client(handler)
    s = await client.get_session("devin-abc")
    assert seen["auth"] == "Bearer cog_test"
    assert seen["path"] == "/v3/organizations/org-x/sessions/devin-abc"
    assert s.session_id == "devin-abc"
    assert s.pull_requests[0].pr_state == "open"
    await client.aclose()


async def test_create_session_body():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=SESSION_JSON)

    client = make_client(handler)
    await client.create_session(
        prompt="do thing",
        repos=["o/r"],
        devin_mode="lite",
        title="t",
        tags=["discord-mobile"],
        max_acu_limit=10,
        create_as_user_id="user_1",
    )
    b = captured["body"]
    assert b["prompt"] == "do thing"
    assert b["repos"] == ["o/r"]
    assert b["devin_mode"] == "lite"
    assert b["tags"] == ["discord-mobile"]
    assert b["max_acu_limit"] == 10
    assert b["create_as_user_id"] == "user_1"
    assert "structured_output_schema" in b
    await client.aclose()


async def test_list_messages_cursor():
    captured = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "items": [
                    {"event_id": "e1", "source": "devin", "message": "hi", "created_at": 1}
                ],
                "end_cursor": "cur1",
                "has_next_page": False,
            },
        )

    client = make_client(handler)
    page = await client.list_messages("devin-abc", after="cur0")
    assert captured["params"]["after"] == "cur0"
    assert page.items[0].source == "devin"
    assert page.end_cursor == "cur1"
    await client.aclose()


async def test_429_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def no_sleep(_):
        pass

    monkeypatch.setattr("devinmobile.devin_client.asyncio.sleep", no_sleep)

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"status": 429, "title": "Too Many Requests"})
        return httpx.Response(200, json=SESSION_JSON)

    client = make_client(handler)
    s = await client.get_session("devin-abc")
    assert calls["n"] == 3
    assert s.status == "running"
    await client.aclose()


async def test_http_error_raises():
    def handler(request):
        return httpx.Response(403, json={"status": 403, "title": "Forbidden"})

    client = make_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_session("devin-abc")
    await client.aclose()

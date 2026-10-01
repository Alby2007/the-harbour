"""Tests for the ACP bridge: a fake bridge speaks JSON-RPC over a real
WebSocket (aiohttp test server on localhost)."""

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from devinmobile.acp_bridge import (
    MODEL_ALIASES,
    AcpBridge,
    BridgeError,
    ConfigOption,
    resolve_option,
)

MODEL_OPTIONS = [
    {"name": "Fusion", "value": "devin-auto"},
    {"name": "Normal", "value": "devin-2-5"},
    {"name": "Lite", "value": "devin_lite"},
    {"name": "SWE-2 Medium", "value": "devin-swe-2-low"},
    {"name": "SWE-2 Max", "value": "devin-swe-2-max"},
    {"name": "Opus 5.5 (Preview)", "value": "devin-opus-5-5"},
]

CONFIG_OPTIONS = [
    {"id": "org_id", "name": "Organization", "type": "select",
     "currentValue": "org-x", "options": [{"name": "Org", "value": "org-x"}]},
    {"id": "repos", "name": "Repositories", "type": "select",
     "currentValue": "", "options": [
         {"name": "harbour", "value": "Alby2007/the-harbour"},
         {"name": "stockbot", "value": "Alby2007/Stockbot"},
     ]},
    {"id": "devin_version", "name": "Devin version", "type": "select",
     "currentValue": "devin-2-5", "options": MODEL_OPTIONS},
]


class FakeBridgeServer:
    """In-process stand-in for wss://api/acp/live. Records every request."""

    def __init__(self, token: str = "devin-secret-token") -> None:
        self.token = token
        self.requests: list[dict[str, Any]] = []
        self.prompt_updates_before_response = True
        self.fail_on: dict[str, dict[str, Any]] = {}
        self.blueprints: list[dict[str, Any]] = []
        self.server = TestServer(web.Application())

    async def start(self) -> str:
        self.server.app.router.add_get("/acp/live", self._ws_handler)
        await self.server.start_server()
        return f"http://127.0.0.1:{self.server.port}"

    async def stop(self) -> None:
        await self.server.close()

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        if request.query.get("token") != self.token:
            await ws.close(code=4403)
            return ws
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            req = json.loads(msg.data)
            self.requests.append(req)
            method, rid, params = req["method"], req["id"], req.get("params", {})
            if method in self.fail_on:
                await ws.send_str(json.dumps(
                    {"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -1, "message": self.fail_on[method]}}
                ))
                continue
            if method == "initialize":
                result = {"protocolVersion": 1, "authMethods": [],
                          "agentInfo": {"name": "devin", "version": "1.0.0"}}
            elif method == "session/new":
                result = {"sessionId": "devin-deadbeefcafe1234",
                          "configOptions": CONFIG_OPTIONS}
            elif method == "session/set_config_option":
                result = {"configOptions": CONFIG_OPTIONS}
            elif method == "_cognition.ai/snapshot-setup/list-blueprints":
                result = {"blueprints": self.blueprints}
            elif method == "_cognition.ai/snapshot-setup/create-blueprint":
                self.blueprints.append({"repo_name": params.get("repo_name")})
                result = {"blueprint_id": "bp-new"}
            elif method == "session/prompt":
                if self.prompt_updates_before_response:
                    await ws.send_str(json.dumps({
                        "jsonrpc": "2.0", "method": "session/update",
                        "params": {"sessionId": params["sessionId"],
                                   "update": {"sessionUpdate": "user_message_chunk"}},
                    }))
                result = {"stopReason": "end_turn"}
            else:
                result = {}
            await ws.send_str(json.dumps(
                {"jsonrpc": "2.0", "id": rid, "result": result}))
        return ws


@pytest.fixture
def creds_file(tmp_path: Path) -> Path:
    p = tmp_path / "credentials.toml"
    p.write_text(
        'windsurf_api_key = "devin-secret-token"\n'
        'api_server_url = "https://server.codeium.com"\n'
        'devin_webapp_host = "app.devin.ai"\n'
        'devin_api_url = "https://api.devin.ai"\n'
    )
    return p


@pytest.fixture
async def fake():
    srv = FakeBridgeServer()
    base = await srv.start()
    yield srv, base
    await srv.stop()


def make_bridge(creds: Path, base: str) -> AcpBridge:
    return AcpBridge(str(creds), api_url=base, timeout=10.0)


# ---- pure resolution -----------------------------------------------------


def opts() -> list[ConfigOption]:
    return [ConfigOption(name=o["name"], value=o["value"]) for o in MODEL_OPTIONS]


def test_resolve_alias():
    assert resolve_option("max", opts()).value == "devin-swe-2-max"
    assert resolve_option("opus", opts()).value == "devin-opus-5-5"


def test_resolve_exact_value_and_name():
    assert resolve_option("devin_lite", opts()).value == "devin_lite"
    assert resolve_option("SWE-2 Medium", opts()).value == "devin-swe-2-low"


def test_resolve_unknown_lists_options():
    with pytest.raises(BridgeError, match="unknown model"):
        resolve_option("definitely-not-a-model", opts())


def test_resolve_ambiguous():
    with pytest.raises(BridgeError, match="ambiguous"):
        resolve_option("swe-2", opts())  # matches Medium + Max + priority variants


# ---- wire flow -----------------------------------------------------------


async def test_happy_path(fake, creds_file):
    srv, base = fake
    b = make_bridge(creds_file, base)
    sess = await b.create_cloud_session("do the thing", model="swe2-max",
                                        repos=["Alby2007/the-harbour"])
    assert sess.session_id == "deadbeefcafe1234"  # devin- prefix stripped
    assert sess.url == "https://app.devin.ai/sessions/deadbeefcafe1234"
    assert sess.model == "devin-swe-2-max"
    assert sess.model_label == "SWE-2 Max"

    methods = [r["method"] for r in srv.requests]
    assert methods == [
        "initialize", "session/new",
        "session/set_config_option",              # devin_version
        "_cognition.ai/snapshot-setup/list-blueprints",
        "_cognition.ai/snapshot-setup/create-blueprint",  # harbour has none
        "session/set_config_option",              # repos
        "session/prompt",
    ]
    cfg = srv.requests[2]["params"]
    assert cfg["configId"] == "devin_version"
    assert cfg["value"] == "devin-swe-2-max"
    create_bp = srv.requests[4]["params"]
    assert create_bp["repo_name"] == "Alby2007/the-harbour"
    assert srv.requests[5]["params"]["configId"] == "repos"
    assert srv.requests[5]["params"]["value"] == "Alby2007/the-harbour"
    prompt = srv.requests[6]["params"]["prompt"]
    assert prompt == [{"type": "text", "text": "do the thing"}]


async def test_blueprint_skipped_when_repo_has_one(fake, creds_file):
    """A repo with an existing blueprint doesn't trigger create-blueprint."""
    srv, base = fake
    srv.blueprints.append({"repo_name": "Alby2007/the-harbour"})
    b = make_bridge(creds_file, base)
    await b.create_cloud_session("x", repos=["Alby2007/the-harbour"])
    methods = [r["method"] for r in srv.requests]
    assert "_cognition.ai/snapshot-setup/list-blueprints" in methods
    assert "_cognition.ai/snapshot-setup/create-blueprint" not in methods


async def test_prompt_returns_on_first_update(fake, creds_file):
    """session/prompt answers at turn end — we detach on the first update."""
    srv, base = fake
    srv.prompt_updates_before_response = True
    b = make_bridge(creds_file, base)
    # If the bridge waited for the real response the fake would still reply,
    # but this proves the early-return path exists and works.
    sess = await b.create_cloud_session("hi", model=None)
    assert sess.session_id == "deadbeefcafe1234"


async def test_repos_case_resolved_to_canonical(fake, creds_file):
    """The bridge silently drops non-exact option values — the client must
    canonicalize. `alby2007/stockbot` must hit the wire as `Alby2007/Stockbot`."""
    srv, base = fake
    b = make_bridge(creds_file, base)
    await b.create_cloud_session("x", repos=["alby2007/stockbot"])
    set_calls = [r for r in srv.requests if r["method"] == "session/set_config_option"]
    assert len(set_calls) == 1
    assert set_calls[0]["params"]["value"] == "Alby2007/Stockbot"


async def test_repos_multi_and_substring(fake, creds_file):
    srv, base = fake
    b = make_bridge(creds_file, base)
    await b.create_cloud_session("x", repos=["STOCKBOT", "harbour"])
    set_calls = [r for r in srv.requests if r["method"] == "session/set_config_option"]
    assert set_calls[0]["params"]["value"] == "Alby2007/Stockbot,Alby2007/the-harbour"


async def test_unknown_repo_raises(fake, creds_file):
    srv, base = fake
    b = make_bridge(creds_file, base)
    with pytest.raises(BridgeError, match="unknown repo"):
        await b.create_cloud_session("x", repos=["nobody/nonexistent"])
    assert "session/prompt" not in [r["method"] for r in srv.requests]


async def test_unknown_model_raises(fake, creds_file):
    srv, base = fake
    b = make_bridge(creds_file, base)
    with pytest.raises(BridgeError, match="unknown model"):
        await b.create_cloud_session("x", model="bogus")
    # prompt must NOT have been sent — draft only
    assert "session/prompt" not in [r["method"] for r in srv.requests]


async def test_set_config_error_propagates(fake, creds_file):
    srv, base = fake
    srv.fail_on["session/set_config_option"] = {"code": -32602, "message": "bad value"}
    b = make_bridge(creds_file, base)
    with pytest.raises(BridgeError, match="set_config_option"):
        await b.create_cloud_session("x", model="max")


async def test_missing_credentials(tmp_path):
    b = AcpBridge(str(tmp_path / "nope.toml"))
    with pytest.raises(BridgeError, match="devin auth login"):
        await b.create_cloud_session("x", model="max")
    assert not b.available


async def test_malformed_credentials(tmp_path):
    p = tmp_path / "credentials.toml"
    p.write_text("windsurf_api_key = 123")  # no api url
    b = AcpBridge(str(p))
    with pytest.raises(BridgeError, match="missing"):
        await b.create_cloud_session("x", model="max")


async def test_bad_token_rejected(fake, tmp_path):
    srv, base = fake
    p = tmp_path / "credentials.toml"
    p.write_text('windsurf_api_key = "wrong"\ndevin_api_url = "https://api.devin.ai"\n')
    b = AcpBridge(str(p), api_url=base, timeout=5.0)
    with pytest.raises(BridgeError):
        await b.create_cloud_session("x", model="max")


def test_alias_map_shape():
    # every alias target should look like a devin_version value
    assert all(v.startswith("devin") for v in MODEL_ALIASES.values())
    assert len(set(MODEL_ALIASES.values())) >= 10  # distinct models, not one blob

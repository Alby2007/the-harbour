"""Create Devin Cloud sessions via the ACP bridge.

The public v3 API exposes ``devin_mode`` but not the per-model picker the
CLI/Desktop offer. Internally those clients speak ACP (Agent Client Protocol)
JSON-RPC over a WebSocket bridge at ``{api_url}/acp/live?token=<cli token>``.
``session/new`` returns ``configOptions`` — selects for ``devin_version``
(the model catalog), ``repos``, ``platform`` and ``persona_slug`` — and
``session/set_config_option`` applies them before the first prompt.

A ``session/new`` session is a draft: it becomes visible to the v3 API (and
thus to our normal poll/steer pipeline) only once ``session/prompt`` lands,
so this module always sends the prompt and waits for the first update.

Auth is the CLI's own credential store, written by ``devin auth login``:
``~/.local/share/devin/credentials.toml`` -> ``windsurf_api_key`` +
``devin_api_url``. The key is long-lived; if the bridge rejects it the fix is
to re-run ``devin auth login`` on this machine.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

DEFAULT_CREDENTIALS_PATH = "~/.local/share/devin/credentials.toml"


class BridgeError(RuntimeError):
    """Raised for any bridge failure — auth, connect, RPC error, timeout."""


@dataclass
class ConfigOption:
    """One entry of a session config select, e.g. a model or repo."""

    name: str
    value: str


@dataclass
class BridgeSession:
    session_id: str  # bare hex id — the devin- prefix is stripped
    url: str
    model: str | None = None  # resolved devin_version value, e.g. devin-swe-2-max
    model_label: str | None = None  # display name, e.g. "SWE-2 Max"


@dataclass
class NewSession:
    session_id: str
    config_options: dict[str, dict[str, Any]] = field(default_factory=dict)


# Friendly aliases -> the option *value* seen in configOptions. Resolution also
# tries exact value/name matches and substrings against the live list, so this
# map is only a convenience layer; unknown-but-valid devin-* values still work.
MODEL_ALIASES: dict[str, str] = {
    "fusion": "devin-auto",
    "auto": "devin-auto",
    "ultra": "devin-ultra",
    "normal": "devin-2-5",
    "default": "devin-2-5",
    "fast": "devin-fast-opus",
    "lite": "devin_lite",
    "swe": "devin-swe-2-max",
    "swe2": "devin-swe-2-max",
    "swe-max": "devin-swe-2-max",
    "swe2-max": "devin-swe-2-max",
    "max": "devin-swe-2-max",
    "swe-high": "devin-swe-2-high",
    "swe2-high": "devin-swe-2-high",
    "high": "devin-swe-2-high",
    "swe-medium": "devin-swe-2-low",
    "swe2-medium": "devin-swe-2-low",
    "medium": "devin-swe-2-low",
    "swe2-medium-priority": "devin-swe-2-priority-low",
    "swe2-high-priority": "devin-swe-2-priority-high",
    "swe2-max-priority": "devin-swe-2-priority-max",
    "opus": "devin-opus-5-5",
    "opus-5.5": "devin-opus-5-5",
    "gpt-6": "devin-gpt-6-sol",
    "gpt6": "devin-gpt-6-sol",
    "gpt-6.1": "devin-gpt-6-1-sol",
    "gpt6.1": "devin-gpt-6-1-sol",
    "gpt-5.6": "devin-gpt-5-6",
}


def resolve_option(
    user_input: str, options: list[ConfigOption], *, what: str = "model"
) -> ConfigOption:
    """Map free text to a config option value.

    Order: alias map -> exact value -> exact name -> substring of name/value.
    Raises BridgeError listing valid choices when nothing matches.
    """
    text = user_input.strip().lower()
    if not text:
        raise BridgeError(f"empty {what}")
    aliased = MODEL_ALIASES.get(text, text) if what == "model" else text
    for opt in options:
        if opt.value.lower() == aliased or opt.value.lower() == text:
            return opt
    for opt in options:
        if opt.name.lower() == aliased or opt.name.lower() == text:
            return opt
    matches = [
        o
        for o in options
        if text in o.name.lower() or text in o.value.lower() or aliased in o.value.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise BridgeError(
            f"ambiguous {what} {user_input!r}: " + ", ".join(o.name for o in matches)
        )
    valid = ", ".join(o.name for o in options[:15]) or "(none advertised)"
    raise BridgeError(f"unknown {what} {user_input!r}. Options: {valid}")


class AcpBridge:
    def __init__(
        self,
        credentials_path: str = DEFAULT_CREDENTIALS_PATH,
        *,
        api_url: str | None = None,
        timeout: float = 45.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._credentials_path = Path(credentials_path).expanduser()
        self._api_url_override = api_url
        self._timeout = timeout
        self._session = session  # injectable for tests
        self._owned_session: aiohttp.ClientSession | None = None
        self._ids = itertools.count(1)
        self._catalog_cache: dict[str, dict[str, Any]] | None = None

    # ---- credentials ----------------------------------------------------

    def _load_credentials(self) -> tuple[str, str]:
        """Return (token, api_url). Raises BridgeError if absent/malformed."""
        try:
            data = tomllib.loads(self._credentials_path.read_text())
        except FileNotFoundError:
            raise BridgeError(
                f"no CLI credentials at {self._credentials_path} — "
                "run `devin auth login` on this machine first"
            ) from None
        except (tomllib.TOMLDecodeError, OSError) as e:
            raise BridgeError(f"unreadable credentials file: {e}") from e
        token = data.get("windsurf_api_key")
        api_url = self._api_url_override or data.get("devin_api_url")
        if not token or not api_url:
            raise BridgeError("credentials.toml missing windsurf_api_key/devin_api_url")
        return token, str(api_url).rstrip("/")

    @property
    def available(self) -> bool:
        return self._credentials_path.exists()

    # ---- JSON-RPC plumbing ----------------------------------------------

    async def _rpc(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
        early_update_for: str | None = None,
    ) -> dict[str, Any]:
        """Send one request; return its result dict.

        Notifications are ignored except when ``early_update_for`` is set:
        the first ``session/update`` for that session id returns early with
        ``{"_early_update": True}`` — used so ``session/prompt`` doesn't have
        to block until the whole turn finishes.
        """
        req_id = next(self._ids)
        await ws.send_str(
            json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        )
        limit = timeout or self._timeout
        try:
            while True:
                msg = await asyncio.wait_for(ws.receive(), limit)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE,
                                  aiohttp.WSMsgType.ERROR):
                    raise BridgeError(f"bridge connection closed during {method}")
                else:
                    continue
                if data.get("id") == req_id:
                    if "error" in data:
                        err = data["error"]
                        raise BridgeError(f"{method}: {err.get('message', err)}")
                    return data.get("result") or {}
                if (
                    early_update_for
                    and data.get("method") == "session/update"
                    and data.get("params", {}).get("sessionId") == early_update_for
                ):
                    return {"_early_update": True}
        except TimeoutError:
            raise BridgeError(f"{method} timed out after {limit:.0f}s") from None

    async def _connect(self) -> aiohttp.ClientWebSocketResponse:
        token, api_url = self._load_credentials()
        ws_url = api_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_url}/acp/live?token={token}"
        try:
            if self._session is not None:
                ws = await self._session.ws_connect(ws_url)
            else:
                self._owned_session = aiohttp.ClientSession()
                ws = await self._owned_session.ws_connect(ws_url)
        except aiohttp.WSServerHandshakeError as e:
            raise BridgeError(
                f"bridge rejected auth ({e.status}) — re-run `devin auth login`"
            ) from e
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            raise BridgeError(f"bridge connect failed: {e}") from e
        await self._rpc(
            ws,
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "devinmobile", "version": "0.1.0"},
            },
        )
        return ws

    async def _close(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            await ws.close()
        finally:
            owned = getattr(self, "_owned_session", None)
            if owned is not None:
                self._owned_session = None
                await owned.close()

    async def _ensure_blueprints(
        self, ws: aiohttp.ClientWebSocketResponse, org_id: str, repos: list[str]
    ) -> None:
        """Ensure a snapshot blueprint exists for each repo.

        The cloud VM clones repos via their blueprint's snapshot; a repo with
        no blueprint silently isn't cloned even when the `repos` config option
        is set. Best-effort: blueprint failures log and continue — the repo
        attach still happens server-side.
        """
        try:
            existing = await self._rpc(
                ws,
                "_cognition.ai/snapshot-setup/list-blueprints",
                {"org_id": org_id},
            )
            have = {
                b.get("repo_name")
                for b in existing.get("blueprints", [])
                if b.get("repo_name")
            }
            for repo in repos:
                if repo in have:
                    continue
                created = await self._rpc(
                    ws,
                    "_cognition.ai/snapshot-setup/create-blueprint",
                    {"org_id": org_id, "repo_name": repo},
                )
                if created.get("blueprint_id"):
                    log.info("created env blueprint for %s", repo)
        except BridgeError as e:
            log.warning("blueprint ensure failed (repo attach may not clone): %s", e)

    # ---- public API ------------------------------------------------------

    async def catalog(self) -> dict[str, dict[str, Any]]:
        """Return the session/new configOptions catalog, cached for the
        process lifetime.

        Spawns an unprompted draft purely to read the options — drafts are
        invisible to v3 and cost nothing. Powers Discord autocomplete for
        repos; the result feeds ``resolve_option`` so users can't send a
        value the bridge would silently drop.
        """
        if self._catalog_cache is not None:
            return self._catalog_cache
        ws = await self._connect()
        try:
            new = await self._rpc(ws, "session/new", {"cwd": "/", "mcpServers": []})
        finally:
            await self._close(ws)
        self._catalog_cache = {
            o.get("id", ""): o for o in new.get("configOptions", []) if o.get("id")
        }
        return self._catalog_cache

    async def create_cloud_session(
        self,
        prompt: str,
        *,
        model: str | None = None,
        repos: list[str] | None = None,
        platform: str | None = None,
    ) -> BridgeSession:
        """Create + configure + prompt a cloud session; return its id.

        The session is a draft until the prompt lands — afterwards it is
        visible through the v3 API like any other session.
        """
        ws = await self._connect()
        try:
            new = await self._rpc(
                ws, "session/new", {"cwd": "/", "mcpServers": []}
            )
            session_id: str = new.get("sessionId", "")
            if not session_id:
                raise BridgeError("session/new returned no sessionId")
            options = {
                o.get("id", ""): o for o in new.get("configOptions", []) if o.get("id")
            }

            resolved_model: ConfigOption | None = None
            if model:
                model_opts = [
                    ConfigOption(name=o.get("name", ""), value=o.get("value", ""))
                    for o in (options.get("devin_version", {}).get("options") or [])
                ]
                if model_opts:
                    resolved_model = resolve_option(model, model_opts, what="model")
                else:
                    # Bridge didn't advertise the list — pass the raw value and
                    # let the server validate it.
                    resolved_model = ConfigOption(name=model, value=model)
                await self._rpc(
                    ws,
                    "session/set_config_option",
                    {
                        "sessionId": session_id,
                        "configId": "devin_version",
                        "value": resolved_model.value,
                    },
                )
            if repos and "repos" in options:
                repo_opts = [
                    ConfigOption(name=o.get("name", ""), value=o.get("value", ""))
                    for o in (options["repos"].get("options") or [])
                ]
                # The bridge silently drops values that aren't exact option
                # values — resolve case-insensitively to the canonical names.
                resolved_repos = (
                    [resolve_option(r, repo_opts, what="repo").value for r in repos]
                    if repo_opts
                    else list(repos)
                )
                # A repo only gets cloned into the workspace if it has an env
                # blueprint — without one the session silently falls back to
                # the default repo. Mirror the CLI's ensure_blueprint_exists.
                org_id = options.get("org_id", {}).get("currentValue") or ""
                if org_id:
                    await self._ensure_blueprints(ws, org_id, resolved_repos)
                await self._rpc(
                    ws,
                    "session/set_config_option",
                    {
                        "sessionId": session_id,
                        "configId": "repos",
                        "value": ",".join(resolved_repos),
                    },
                )
            if platform and "platform" in options:
                await self._rpc(
                    ws,
                    "session/set_config_option",
                    {
                        "sessionId": session_id,
                        "configId": "platform",
                        "value": platform,
                    },
                )

            await self._rpc(
                ws,
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                },
                timeout=self._timeout,
                early_update_for=session_id,
            )
            bare_id = session_id.removeprefix("devin-")
            return BridgeSession(
                session_id=bare_id,
                url=f"https://app.devin.ai/sessions/{bare_id}",
                model=resolved_model.value if resolved_model else None,
                model_label=resolved_model.name if resolved_model else None,
            )
        finally:
            await self._close(ws)

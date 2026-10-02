import asyncio
import logging
import random
from typing import Any

import httpx

from .models import MessagePage, Session

log = logging.getLogger(__name__)

# Asked of every session so the completion embed can render a structured
# summary without scraping message text.
DEFAULT_STRUCTURED_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "What was done, in a few sentences."},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests_passed": {"type": ["boolean", "null"]},
        "notes": {"type": "string", "description": "Follow-ups, risks, anything worth knowing."},
        # playbook chains read this as a gate: false = no follow-up phase
        # is worth running (prompt must opt in — set only when asked)
        "proceed": {
            "type": ["boolean", "null"],
            "description": "false when there is no follow-up work worth doing",
        },
        # harvested into the repo_notes table on completion — the loop that
        # lets one session teach the next (relay._harvest_repo_notes)
        "repo_notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Repo-specific facts future sessions should know "
                "(flaky tests, setup quirks, conventions)"
            ),
        },
    },
    "required": ["summary"],
    "additionalProperties": True,
}

SESSION_TAG = "discord-mobile"


def _items(data: Any, *keys: str) -> list[dict[str, Any]]:
    """v3 list endpoints wrap rows inconsistently — accept a bare list or
    any of the usual envelope keys."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("items", *keys):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


class DevinClient:
    def __init__(
        self,
        api_key: str,
        org_id: str,
        base_url: str = "https://api.devin.ai/v3",
        *,
        timeout: float = 30.0,
        max_retries: int = 4,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._org_id = org_id
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _path(self, suffix: str) -> str:
        return f"/organizations/{self._org_id}{suffix}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        resp: httpx.Response | None = None
        for attempt in range(self._max_retries):
            resp = await self._client.request(method, path, **kwargs)
            if resp.status_code != 429 and resp.status_code < 500:
                resp.raise_for_status()
                return resp
            delay = min(2.0**attempt + random.random(), 30.0)
            log.warning(
                "devin %s %s -> %s, retrying in %.1fs",
                method, path, resp.status_code, delay,
            )
            await asyncio.sleep(delay)
        assert resp is not None
        resp.raise_for_status()
        return resp

    async def create_session(
        self,
        *,
        prompt: str,
        repos: list[str] | None = None,
        devin_mode: str | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        max_acu_limit: int | None = None,
        create_as_user_id: str | None = None,
        structured_output_schema: dict[str, Any] | None = DEFAULT_STRUCTURED_OUTPUT_SCHEMA,
        attachment_urls: list[str] | None = None,
        bypass_approval: bool | None = None,
        session_secrets: list[dict[str, str]] | None = None,
        playbook_id: str | None = None,
    ) -> Session:
        body: dict[str, Any] = {"prompt": prompt}
        if repos:
            body["repos"] = repos
        if devin_mode:
            body["devin_mode"] = devin_mode
        if title:
            body["title"] = title
        if tags:
            body["tags"] = tags
        if max_acu_limit is not None:
            body["max_acu_limit"] = max_acu_limit
        if create_as_user_id:
            body["create_as_user_id"] = create_as_user_id
        if structured_output_schema:
            body["structured_output_schema"] = structured_output_schema
        if attachment_urls:
            body["attachment_urls"] = attachment_urls
        if bypass_approval is not None:
            body["bypass_approval"] = bypass_approval
        if session_secrets:
            # verified field (422 on wrong shape): literal per-session env
            # injection for secrets that must NOT be org-wide
            body["session_secrets"] = session_secrets
        if playbook_id:
            # verified end-to-end: the playbook body lands as standing
            # instructions on the session
            body["playbook_id"] = playbook_id
        resp = await self._request("POST", self._path("/sessions"), json=body)
        return Session.model_validate(resp.json())

    async def get_session(self, session_id: str) -> Session:
        resp = await self._request("GET", self._path(f"/sessions/{session_id}"))
        return Session.model_validate(resp.json())

    async def send_message(
        self,
        session_id: str,
        message: str,
        *,
        attachment_urls: list[str] | None = None,
        message_as_user_id: str | None = None,
    ) -> Session:
        body: dict[str, Any] = {"message": message}
        if attachment_urls:
            body["attachment_urls"] = attachment_urls
        if message_as_user_id:
            body["message_as_user_id"] = message_as_user_id
        resp = await self._request(
            "POST", self._path(f"/sessions/{session_id}/messages"), json=body
        )
        return Session.model_validate(resp.json())

    async def terminate_session(self, session_id: str) -> None:
        """DELETE the session — stops ACU burn on /kill. If the API doesn't
        support it (unverified), the caller's park+archive is the fallback."""
        resp = await self._request(
            "DELETE", self._path(f"/sessions/{session_id}")
        )
        resp.raise_for_status()

    async def list_messages(
        self, session_id: str, *, after: str | None = None, first: int = 200
    ) -> MessagePage:
        params: dict[str, Any] = {"first": first}
        if after:
            params["after"] = after
        resp = await self._request(
            "GET", self._path(f"/sessions/{session_id}/messages"), params=params
        )
        return MessagePage.model_validate(resp.json())

    # ---- org secrets (write-only — no value read-back exists on the API) --

    async def list_secrets(self) -> list[dict[str, Any]]:
        """GET /secrets — metadata rows only (secret_id, key, note, types).
        The API serves no value field anywhere."""
        resp = await self._request("GET", self._path("/secrets"))
        return _items(resp.json(), "secrets")

    async def create_secret(
        self,
        key: str,
        value: str,
        *,
        type: str = "key-value",
        note: str | None = None,
    ) -> dict[str, Any]:
        """POST /secrets. An access_type:'org' secret auto-injects into
        EVERY session's env — no per-session grant step (probe finding)."""
        body: dict[str, Any] = {"key": key, "value": value, "type": type}
        if note:
            body["note"] = note
        resp = await self._request("POST", self._path("/secrets"), json=body)
        return resp.json()

    async def delete_secret_by_key(self, key: str) -> bool:
        """No key-addressed DELETE exists — resolve key → secret_id via the
        list first. False when the key isn't there."""
        for s in await self.list_secrets():
            if s.get("key") == key and s.get("secret_id"):
                await self._request(
                    "DELETE", self._path(f"/secrets/{s['secret_id']}")
                )
                return True
        return False

    # ---- playbooks (stored instruction-bodies, not orchestration) --------

    async def list_playbooks(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", self._path("/playbooks"))
        return _items(resp.json(), "playbooks")

    async def create_playbook(
        self, title: str, body: str
    ) -> dict[str, Any]:
        resp = await self._request(
            "POST", self._path("/playbooks"),
            json={"title": title, "body": body},
        )
        return resp.json()

    async def update_playbook(
        self, playbook_id: str, *, title: str, body: str
    ) -> dict[str, Any]:
        """Full replace — PATCH is 405 on this surface (probe)."""
        resp = await self._request(
            "PUT", self._path(f"/playbooks/{playbook_id}"),
            json={"title": title, "body": body},
        )
        return resp.json()

    async def delete_playbook(self, playbook_id: str) -> None:
        await self._request("DELETE", self._path(f"/playbooks/{playbook_id}"))

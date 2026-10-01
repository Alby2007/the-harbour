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
    },
    "required": ["summary"],
    "additionalProperties": True,
}

SESSION_TAG = "discord-mobile"


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

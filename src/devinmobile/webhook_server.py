"""Optional GitHub webhook receiver — pushes PR/check events into bound
threads instantly instead of waiting for the next poll tick.

Runs only when GITHUB_WEBHOOK_SECRET is set; the bot then listens on
GITHUB_WEBHOOK_PORT for `POST /github`. Signature is verified with the
HMAC-SHA256 `X-Hub-Signature-256` header. Events for PRs we don't track
(no prs row) are ignored — single-user bot, no need for a repo map.

The host must be publicly reachable for real GitHub deliveries; locally a
tunnel (cloudflared/ngrok) pointed at the port works fine. Polling in
relay.py already covers state transitions, so this is a latency nicety,
not a requirement.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import TYPE_CHECKING

import discord
from aiohttp import web

from .config import Settings
from .db import Database, PrRow

if TYPE_CHECKING:
    from .bot.main import DevinMobileBot

log = logging.getLogger(__name__)


class WebhookServer:
    def __init__(self, bot: DevinMobileBot, db: Database, settings: Settings) -> None:
        self.bot = bot
        self.db = db
        self.secret = settings.github_webhook_secret.encode()
        self.port = settings.github_webhook_port
        self._runner: web.AppRunner | None = None

    def _verify(self, body: bytes, signature: str | None) -> bool:
        if not signature or not signature.startswith("sha256="):
            return False
        digest = hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, signature[7:])

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/github", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        log.info("github webhook receiver on :%d/github", self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        if not self._verify(body, request.headers.get("X-Hub-Signature-256")):
            return web.Response(status=401)
        try:
            payload = json.loads(body)
        except ValueError:
            return web.Response(status=400)
        event = request.headers.get("X-GitHub-Event", "")
        try:
            if event == "pull_request":
                await self._on_pull_request(payload)
            elif event in ("check_run", "check_suite"):
                await self._on_check(payload)
        except Exception:
            log.exception("webhook %s handling failed", event)
        return web.Response(status=204)

    # ---- events -----------------------------------------------------------

    async def _on_pull_request(self, payload: dict) -> None:
        repo = payload.get("repository", {})
        pr = payload.get("pull_request", {})
        owner = repo.get("owner", {}).get("login", "")
        found = await self.db.binding_for_pr(
            owner, repo.get("name", ""), int(pr.get("number") or 0)
        )
        if found is None:
            return  # not one of ours
        binding, row = found
        state = "merged" if pr.get("merged") else pr.get("state", row.state)
        row.pr_title = pr.get("title") or row.pr_title
        await self._apply(binding, row, state=state)

    async def _on_check(self, payload: dict) -> None:
        # check_run/check_suite payloads carry the associated PRs directly.
        runs = payload.get("check_run") or payload.get("check_suite") or {}
        prs = runs.get("pull_requests") or []
        repo = payload.get("repository", {})
        for p in prs:
            found = await self.db.binding_for_pr(
                repo.get("owner", {}).get("login", ""),
                repo.get("name", ""),
                int(p.get("number") or 0),
            )
            if found is None:
                continue
            binding, row = found
            # A single completed run isn't the aggregate — let the poll
            # reconcile; we only post the completion event.
            conclusion = runs.get("conclusion") or runs.get("status") or "?"
            name = runs.get("name") or "check"
            await self._post(
                binding,
                f"Check `{name}` on PR #{row.number}: **{conclusion}**",
                mention=conclusion in ("failure", "timed_out", "action_required"),
            )

    async def _apply(
        self, binding, row: PrRow, *, state: str | None = None,
        checks: str | None = None,
    ) -> None:
        state = state or row.state
        if state == row.state and checks == row.checks_state:
            return
        row.state, row.checks_state = state, checks or row.checks_state
        await self.db.upsert_pr(row)
        mention = state == "merged"  # merge is the payoff — ping
        await self._post(
            binding, f"PR #{row.number} → **{state}**", mention=mention
        )

    async def _post(self, binding, text: str, *, mention: bool = False) -> None:
        try:
            chan = self.bot.get_channel(binding.thread_id) or (
                await self.bot.fetch_channel(binding.thread_id)
            )
            if not isinstance(chan, discord.abc.Messageable):
                return
            prefix = ""
            if mention:
                prefix = " ".join(
                    f"<@{u}>" for u in self.bot.settings.allowed_user_id_set
                ) + " "
            await chan.send(prefix + text)
        except Exception:
            log.warning("webhook post failed for thread %s", binding.thread_id)


async def maybe_start(
    bot: DevinMobileBot, db: Database, settings: Settings
) -> WebhookServer | None:
    if not settings.github_enabled or not settings.github_webhook_secret:
        return None
    srv = WebhookServer(bot, db, settings)
    try:
        await srv.start()
    except OSError as e:
        log.warning("webhook server failed to bind :%d — %s", srv.port, e)
        return None
    return srv

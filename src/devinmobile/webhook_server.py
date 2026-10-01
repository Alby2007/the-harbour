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
            elif event == "issues":
                await self._on_issue(payload)
            elif event in ("pull_request_review", "pull_request_review_comment"):
                await self._on_review(payload)
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
            # Only failure-ish conclusions post — a chatty CI could otherwise
            # spam the thread once per completed run. Success transitions
            # arrive via the poll's CI-rollup diff instead.
            conclusion = runs.get("conclusion") or ""
            if conclusion not in ("failure", "timed_out", "action_required",
                                  "cancelled"):
                continue
            name = runs.get("name") or "check"
            await self._post(
                binding,
                f"Check `{name}` on PR #{row.number}: **{conclusion}**",
                mention=True,
            )

    async def _on_issue(self, payload: dict) -> None:
        """`devin` label on an issue spawns a session — the issue tracker
        becomes the mobile task queue."""
        if payload.get("action") != "labeled":
            return
        label = (payload.get("label") or {}).get("name") or ""
        if label.lower() != self.bot.settings.github_trigger_label.lower():
            return
        issue = payload.get("issue") or {}
        repo = payload.get("repository") or {}
        owner = (repo.get("owner") or {}).get("login") or ""
        name = repo.get("name") or ""
        if not (owner and name and issue.get("number")):
            return
        if (issue.get("pull_request")):
            return  # PRs arrive through their own events
        from .spawn import SpawnError, spawn_session  # local: import cycle

        title = issue.get("title") or f"issue #{issue['number']}"
        body = (issue.get("body") or "")[:4000]
        url = issue.get("html_url") or ""
        prompt = (
            f"Work on GitHub issue {owner}/{name}#{issue['number']}:\n\n"
            f"**{title}**\n\n{body}\n\n{url}"
        )
        try:
            _, thread = await spawn_session(
                self.bot, prompt=prompt, repos=[f"{owner}/{name}"],
                title=title,
            )
            await thread.send(
                f"Spawned by `{label}` label on {owner}/{name}#{issue['number']}."
            )
        except SpawnError as e:
            log.warning("label trigger spawn failed for %s/%s#%s: %s",
                        owner, name, issue.get("number"), e)

    async def _on_review(self, payload: dict) -> None:
        """PR review events → excerpt into the owning thread with a
        Send-to-Devin button (the button re-fetches feedback at click time,
        so nothing here needs persisting)."""
        repo = payload.get("repository") or {}
        pr = payload.get("pull_request") or {}
        found = await self.db.binding_for_pr(
            (repo.get("owner") or {}).get("login", ""),
            repo.get("name", ""),
            int(pr.get("number") or 0),
        )
        if found is None:
            return
        binding, row = found
        rev = payload.get("review") or payload.get("comment") or {}
        body = (rev.get("body") or "").strip()
        author = ((rev.get("user") or {}).get("login")) or "someone"
        if not body or author.endswith("[bot]"):
            return
        from .views import ReviewNotifyView  # local: views -> discord only, fine

        where = f" on `{rev['path']}`" if rev.get("path") else ""
        await self._post(
            binding,
            f"**{author}** reviewed {row.repo}#{row.number}{where}:\n"
            f"> {body[:500]}",
            view=ReviewNotifyView(
                binding.session_id,
                f"{row.owner}/{row.repo}#{row.number}",
                self.bot.handle_component,
            ),
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

    async def _post(
        self, binding, text: str, *, mention: bool = False,
        view: discord.ui.View | None = None,
    ) -> None:
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
            await chan.send(prefix + text, view=view or discord.utils.MISSING)
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

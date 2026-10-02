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
import math
from typing import TYPE_CHECKING

import discord
from aiohttp import web

from .config import Settings
from .db import Database, PrRow
from .embeds import mention_for

if TYPE_CHECKING:
    from .bot.main import DevinMobileBot

log = logging.getLogger(__name__)


class WebhookServer:
    def __init__(self, bot: DevinMobileBot, db: Database, settings: Settings) -> None:
        self.bot = bot
        self.db = db
        self.secret = settings.github_webhook_secret.encode()
        self.task_token = settings.task_intake_token
        # token → caller-name map (TASK_INTAKE_TOKENS) — a mapped token's
        # session attributes to the name instead of the bare "intake"
        self.task_token_map = settings.task_intake_token_map
        self.port = settings.github_webhook_port
        self._runner: web.AppRunner | None = None

    def _verify(self, body: bytes, signature: str | None) -> bool:
        if not signature or not signature.startswith("sha256="):
            return False
        digest = hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, signature[7:])

    async def start(self) -> None:
        app = web.Application()
        routes = []
        if self.secret:
            app.router.add_post("/github", self._handle)
            routes.append("/github")
        if self.task_token or self.task_token_map:
            app.router.add_post("/task", self._handle_task)
            routes.append("/task")
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        log.info("webhook/intake receiver on :%d%s", self.port, routes)

    async def _handle_task(self, request: web.Request) -> web.Response:
        """POST /task — bearer-authed intake so anything that can curl
        (Siri Shortcuts, Raycast, another bot) can spawn a session.
        Discord becomes the renderer, not the only source."""
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        # bytes compare_digest — a non-ASCII bearer would TypeError on str
        mapped = next(
            (
                name
                for t, name in self.task_token_map.items()
                if hmac.compare_digest(token.encode(), t.encode())
            ),
            None,
        )
        if mapped is None and not (
            self.task_token
            and hmac.compare_digest(token.encode(), self.task_token.encode())
        ):
            return web.Response(status=401)
        try:
            payload = await request.json()
        except ValueError:
            return web.Response(status=400)
        if not isinstance(payload, dict):
            return web.Response(status=400)
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
            return web.Response(status=400)
        # present-but-wrong-type is a 400 — silently dropping a caller's
        # repo/budget would spawn a different task than they asked for
        raw_repo = payload.get("repo")
        if raw_repo is None:
            repos = None
        elif isinstance(raw_repo, str):
            repos = [r.strip() for r in raw_repo.split(",") if r.strip()]
        elif isinstance(raw_repo, list) and all(
            isinstance(r, str) for r in raw_repo
        ):
            repos = [r.strip() for r in raw_repo if r.strip()]
        else:
            return web.Response(status=400)
        title = payload.get("title")
        if title is not None and not isinstance(title, str):
            return web.Response(status=400)
        budget = payload.get("budget")
        # bool is an int subclass — True would cap the task at 1 ACU.
        # json.loads accepts NaN/Infinity literals, and float() of a
        # huge int overflows — all of those are a 400, not a spawn.
        if budget is not None:
            try:
                if isinstance(budget, bool) or not isinstance(
                    budget, (int, float)
                ):
                    raise ValueError
                budget_f = float(budget)
                if not (math.isfinite(budget_f) and budget_f > 0):
                    raise ValueError
            except (ValueError, OverflowError):
                return web.Response(status=400)
        else:
            budget_f = None
        # caller-supplied URLs — Devin must be able to fetch them publicly
        # (no Discord-CDN guarantee for external clients), hence the
        # http(s) scheme gate.
        raw_atts = payload.get("attachments")
        if raw_atts is None:
            attachment_urls = None
        elif (
            isinstance(raw_atts, list)
            and len(raw_atts) <= 8
            and all(
                isinstance(u, str)
                and len(u) <= 2048
                and u.startswith(("http://", "https://"))
                for u in raw_atts
            )
        ):
            attachment_urls = raw_atts or None  # [] == absent
        else:
            return web.Response(status=400)
        # spawned_by precedence: token-map name → `by:` field → "intake".
        # A mapped token IGNORES by: — the mapping is the stronger claim,
        # so the field isn't even validated under it.
        spawned_by = mapped if mapped is not None else "intake"
        if mapped is None:
            by = payload.get("by")
            if by is not None:
                if not isinstance(by, str) or not by.strip() or len(by) > 64:
                    return web.Response(status=400)
                spawned_by = by.strip()
        from .spawn import SpawnError, spawn_session  # local: import cycle

        try:
            session, thread = await spawn_session(
                self.bot,
                prompt=prompt,
                repos=repos or None,
                title=title,
                budget=budget_f,
                attachment_urls=attachment_urls,
                spawned_by=spawned_by,
            )
        except SpawnError as e:
            return web.json_response({"error": str(e)}, status=502)
        except Exception:
            log.exception("task intake spawn crashed")
            return web.json_response({"error": "spawn crashed"}, status=500)
        # best-effort provenance note — a send failure must NOT 500 the
        # caller into a retry that double-spawns a paid session
        try:
            await thread.send("Spawned via `/task` intake.")
        except Exception:
            log.warning("intake note failed for %s", session.session_id)
        guild_id = getattr(getattr(thread, "guild", None), "id", None)
        return web.json_response({
            "session_id": session.session_id,
            "thread_id": str(thread.id),
            "session_url": session.url,
            # tappable deep-link into the live thread (Siri → Discord)
            "thread_url": (
                f"https://discord.com/channels/{guild_id or '@me'}/{thread.id}"
            ),
        })

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
        # the review label rides the same event type — check it before the
        # "not one of ours" early-return (reviewed PRs belong to humans)
        if payload.get("action") == "labeled":
            await self._on_pr_label(payload)
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
                title=title, spawned_by="github",
            )
            await thread.send(
                f"Spawned by `{label}` label on {owner}/{name}#{issue['number']}."
            )
        except SpawnError as e:
            log.warning("label trigger spawn failed for %s/%s#%s: %s",
                        owner, name, issue.get("number"), e)

    async def _on_pr_label(self, payload: dict) -> None:
        """`devin-review` label on a PR spawns a review session — Devin as
        reviewer instead of author. One session per PR via the review_of
        binding column; findings land in the thread, posting them back to
        GitHub is a button press on the completion card."""
        label = (payload.get("label") or {}).get("name") or ""
        if label.lower() != self.bot.settings.github_review_label.lower():
            return
        pr = payload.get("pull_request") or {}
        repo = payload.get("repository") or {}
        owner = (repo.get("owner") or {}).get("login") or ""
        name = repo.get("name") or ""
        number = int(pr.get("number") or 0)
        author = ((pr.get("user") or {}).get("login")) or ""
        if not (owner and name and number):
            return
        if author.endswith("[bot]"):
            return  # don't auto-review Devin's own PRs
        key = f"{owner}/{name}#{number}"
        if await self.db.binding_by_review_of(key) is not None:
            return  # already reviewed/ing — label re-application is a no-op
        from .spawn import SpawnError, spawn_session  # local: import cycle

        prompt = (
            f"Review pull request {key}: \"{pr.get('title') or ''}\".\n\n"
            f"PR description:\n{(pr.get('body') or '')[:3000]}\n\n"
            f"In repo {owner}/{name} read the diff "
            f"(`gh pr diff {number} --repo {owner}/{name}` or the API) and "
            f"review for bugs, security issues, and regressions. Report "
            f"findings as a numbered list with file:line references; if the "
            f"change is clean, say so plainly. Do not modify the PR."
        )
        try:
            _, thread = await spawn_session(
                self.bot, prompt=prompt, repos=[f"{owner}/{name}"],
                title=f"Review {name}#{number}", review_of=key,
                spawned_by="github",
            )
            await thread.send(
                f"Spawned by `{label}` label on {key} — review findings will "
                "land here; the completion card can post them to GitHub."
            )
        except SpawnError as e:
            log.warning("review-label spawn failed for %s: %s", key, e)

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
                prefix = mention_for(
                    binding.spawned_by,
                    self.bot.settings.allowed_user_id_set,
                ) + " "
            await chan.send(prefix + text, view=view or discord.utils.MISSING)
        except Exception:
            log.warning("webhook post failed for thread %s", binding.thread_id)


async def maybe_start(
    bot: DevinMobileBot, db: Database, settings: Settings
) -> WebhookServer | None:
    want_github = settings.github_enabled and settings.github_webhook_secret
    if not (want_github or settings.task_intake_token
            or settings.task_intake_token_map):
        return None
    srv = WebhookServer(bot, db, settings)
    try:
        await srv.start()
    except OSError as e:
        log.warning("webhook server failed to bind :%d — %s", srv.port, e)
        return None
    return srv

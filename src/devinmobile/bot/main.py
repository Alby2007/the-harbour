import asyncio
import logging

import discord
from discord import app_commands

from ..acp_bridge import AcpBridge, SessionStream
from ..config import Settings
from ..db import Database
from ..devin_client import DevinClient
from ..embeds import status_embed
from ..github_client import GithubClient, PullRef, parse_issue_ref
from ..links import enrich as enrich_links
from ..relay import Relay
from ..scheduler import Scheduler
from ..views import MergeConfirmView, dispatch
from ..webhook_server import WebhookServer, maybe_start
from .commands import register_commands

log = logging.getLogger(__name__)


class DevinMobileBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.settings = settings
        self.db: Database
        self.devin: DevinClient
        self.bridge: AcpBridge
        self.github: GithubClient | None = None
        self.relay: Relay
        self._relay_task: asyncio.Task | None = None
        self._webhook: WebhookServer | None = None
        self._scheduler: Scheduler | None = None
        self._stream: SessionStream | None = None

    async def setup_hook(self) -> None:
        self.db = await Database.connect(self.settings.db_path)
        self.devin = DevinClient(
            self.settings.devin_api_key,
            self.settings.devin_org_id,
            self.settings.devin_base_url,
        )
        self.bridge = AcpBridge(
            self.settings.devin_credentials_path,
            api_url=self.settings.devin_api_url_override,
            timeout=self.settings.bridge_timeout,
        )
        if not self.bridge.available:
            log.warning(
                "no CLI credentials at %s — the model: option on /devin is disabled "
                "until `devin auth login` runs on this host",
                self.settings.devin_credentials_path,
            )
        else:
            # Warm the repo/model catalog so slash-command autocomplete is
            # instant — the first keystroke shouldn't pay a WS round-trip.
            async def _warm_catalog() -> None:
                try:
                    await self.bridge.catalog()
                except Exception:
                    log.warning("bridge catalog prefetch failed", exc_info=True)

            asyncio.create_task(_warm_catalog())
        if self.settings.github_enabled:
            self.github = GithubClient(
                self.settings.github_app_id,
                self.settings.github_app_private_key_path,
                self.settings.github_app_installation_id,
            )
        elif any([
            self.settings.github_app_id,
            self.settings.github_app_private_key_path,
            self.settings.github_app_installation_id,
        ]):
            log.warning("partial GITHUB_APP_* config — GitHub features disabled")
        self.relay = Relay(
            self, self.devin, self.db, self.settings, self.github,
            component_handler=self.handle_component,
        )
        if self.bridge.available and self.settings.acp_progress:
            # Live turn progress via the bridge's session/update stream.
            self._stream = SessionStream(self.bridge, on_update=self.relay.on_progress)
            self.relay.streamer = self._stream
        self._scheduler = Scheduler(self)
        self._scheduler.start()
        self._webhook = await maybe_start(self, self.db, self.settings)
        register_commands(self)
        if self.settings.command_guild_id:
            guild = discord.Object(id=self.settings.command_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        await self.tree.sync()  # global: makes commands usable in DMs
        self._relay_task = asyncio.create_task(self.relay.run_forever())

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("online as %s (%s)", self.user, self.user.id)

    async def close(self) -> None:
        if self._relay_task:
            self._relay_task.cancel()
        if self.relay:
            await self.relay.aclose()
        if self.db:
            await self.db.close()
        if self.devin:
            await self.devin.aclose()
        if self.github:
            await self.github.aclose()
        if self._webhook:
            await self._webhook.stop()
        if self._scheduler:
            await self._scheduler.stop()
        await super().close()

    # ---- steering: anything an allowed user types in a bound thread goes to
    # the session; attachments ride along as attachment_urls.

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or (not message.content and not message.attachments):
            return
        binding = await self.db.get_binding_by_thread(message.channel.id)
        if binding is None:
            return
        if message.author.id not in self.settings.allowed_user_id_set:
            return
        content = message.content
        attachments = [a.url for a in message.attachments] or None
        if not content and message.attachments and self.settings.openai_api_key:
            # Voice notes land as audio/* attachments — transcribe and steer.
            from ..transcribe import transcribe  # local import: httpx lazily

            voice = next(
                (a for a in message.attachments
                 if (a.content_type or "").startswith("audio/")
                 or getattr(a, "waveform", None)),
                None,
            )
            if voice is not None:
                try:
                    content = await transcribe(voice.url, self.settings.openai_api_key)
                    attachments = [
                        a.url for a in message.attachments if a is not voice
                    ] or None
                    if content:
                        await message.reply(f"🎤 _{content}_", mention_author=False)
                except Exception:
                    log.exception("voice transcription failed")
                    await message.add_reaction("🎤")
                    return
        n_links = 0
        if content and ("http://" in content or "https://" in content):
            # Fetch link contents here — Devin's sandbox browser can't reach
            # private GitHub (our App can) or anything paywalled, so the
            # extracted text rides inside the prompt.
            try:
                content, n_links = await enrich_links(content, self.github)
            except Exception:
                log.exception("link enrichment failed")
        try:
            session = await self.devin.send_message(
                binding.session_id,
                content or "(attachment)",
                attachment_urls=attachments,
                message_as_user_id=self.settings.create_as_user_id,
            )
            # Same lock as the poll loop — an in-flight poll can't overwrite
            # this reactivation with a stale parked status.
            async with self.relay._lock(binding.session_id):
                binding.status = session.status
                binding.status_detail = session.status_detail
                binding.active = True
                await self.db.upsert_binding(binding)
            await message.add_reaction("\u2705")
            if n_links:
                await message.channel.send(
                    f"📎 Read {n_links} link{'s' if n_links > 1 else ''} for Devin"
                )
            # Placeholder covers the send→first-output dead air; it morphs
            # into the Working list on the first streamed tool call and is
            # deleted when the reply lands.
            await self.relay.progress.thinking(binding)
            # Devin's ack typically lands within seconds — don't make the
            # reply wait for the next scheduled tick.
            self.relay.request_poll(binding)
        except Exception:
            log.exception("forward failed for %s", binding.session_id)
            await message.add_reaction("\u274C")

    # ---- component clicks (post-restart path; live views dedupe via _INFLIGHT)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        await dispatch(interaction, self.handle_component)

    async def handle_component(
        self,
        interaction: discord.Interaction,
        action: str,
        session_id: str,
        extra: str | None = None,
    ) -> None:
        if interaction.user.id not in self.settings.allowed_user_id_set:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "This bot is locked to its owner.", ephemeral=True
                )
            return
        binding = await self.db.get_binding(session_id)
        if action.startswith("pr_"):
            await self._handle_pr_action(interaction, action, session_id, extra)
            return
        if action == "ssh":
            await interaction.response.send_message(
                f"```\nssh {session_id}@ssh.devin.ai\n```\n"
                f"CLI: `devin ssh {session_id}` · "
                f"forward a port: `devin forward {session_id} 3000`\n"
                "The gateway prints a URL — open it in a browser to approve the connection.",
                ephemeral=True,
            )
        elif action == "refresh":
            await interaction.response.defer()
            try:
                session = await self.devin.get_session(session_id)
            except Exception as e:  # noqa: BLE001 — surface the API error
                await interaction.followup.send(f"Refresh failed: `{e}`", ephemeral=True)
                return
            if interaction.message:
                await interaction.message.edit(
                    embed=status_embed(
                        session, fallback_title=binding.title if binding else None
                    )
                )
            if binding:
                binding.status = session.status
                binding.status_detail = session.status_detail
                await self.db.upsert_binding(binding)
        elif action == "approve":
            await interaction.response.defer(ephemeral=True)
            try:
                await self.devin.send_message(
                    session_id,
                    "Approved — please proceed.",
                    message_as_user_id=self.settings.create_as_user_id,
                )
            except Exception as e:  # noqa: BLE001
                await interaction.followup.send(f"Approve failed: `{e}`", ephemeral=True)
                return
            await interaction.followup.send("Sent an approval to the session.", ephemeral=True)

    async def _handle_pr_action(
        self,
        interaction: discord.Interaction,
        action: str,
        session_id: str,
        pr_key: str | None,
    ) -> None:
        if self.github is None or not pr_key:
            await interaction.response.send_message(
                "GitHub App isn't configured (GITHUB_APP_*).", ephemeral=True
            )
            return
        ref_parsed = parse_issue_ref(pr_key)
        if ref_parsed is None:
            await interaction.response.send_message(
                f"Bad PR reference {pr_key!r}.", ephemeral=True
            )
            return
        o, r, n = ref_parsed
        row = await self.db.get_pr_by_ref(session_id, o, r, n)
        if row is None:
            await interaction.response.send_message(
                f"No PR {pr_key} tracked for this session.", ephemeral=True
            )
            return
        ref = PullRef(owner=row.owner, repo=row.repo, number=row.number)

        if action == "pr_merge":
            # first click just asks for confirmation — the real merge happens
            # on pr_merge_go below
            await interaction.response.send_message(
                f"Merge **{row.pr_title or row.pr_url}** "
                f"({self.settings.github_merge_method})?",
                view=MergeConfirmView(session_id, pr_key, self.handle_component),
                ephemeral=True,
            )
            return
        if action == "pr_automerge":
            row.auto_merge = not row.auto_merge
            await self.db.upsert_pr(row)
            state = "ON — merges when CI goes green" if row.auto_merge else "off"
            if interaction.message:
                binding = await self.db.get_binding(session_id)
                if binding is not None:
                    try:
                        await self.relay._refresh_pr_card(binding, row)
                    except Exception:  # noqa: BLE001
                        pass
            await interaction.response.send_message(
                f"Auto-merge {state} for {ref.key}.", ephemeral=True
            )
            return
        if action == "fix_ci":
            await interaction.response.defer(ephemeral=True)
            try:
                fails = await self.github.get_failed_checks(ref)
                lines = "\n".join(
                    f"- {f['name']}: {f['conclusion']} ({f['url']})"
                    for f in fails[:10]
                ) or "- (no failed check runs found — inspect the PR)"
                await self.devin.send_message(
                    session_id,
                    f"CI failed on {ref.key} — please investigate and fix:\n{lines}",
                    message_as_user_id=self.settings.create_as_user_id,
                )
            except Exception as e:  # noqa: BLE001
                await interaction.followup.send(f"Failed: `{e}`", ephemeral=True)
                return
            await interaction.followup.send(
                "Sent the failing checks to Devin.", ephemeral=True
            )
            return
        if action == "send_review":
            await interaction.response.defer(ephemeral=True)
            try:
                feedback = await self.github.get_review_feedback(ref)
                lines = "\n".join(
                    f"- {f['author']}"
                    + (f" on `{f['path']}`" if f.get("path") else "")
                    + (f" [{f['state']}]" if f.get("state") else "")
                    + f": {f['body'][:400]}"
                    for f in feedback
                ) or "- (no review comments found)"
                await self.devin.send_message(
                    session_id,
                    f"Review feedback on {ref.key} — please address it:\n{lines}",
                    message_as_user_id=self.settings.create_as_user_id,
                )
            except Exception as e:  # noqa: BLE001
                await interaction.followup.send(f"Failed: `{e}`", ephemeral=True)
                return
            await interaction.followup.send(
                "Sent the review feedback to Devin.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            if action == "pr_merge_go":
                await self.github.merge_pr(ref, method=self.settings.github_merge_method)
                row.state = "merged"
                msg = f"Merged {ref.key}."
            elif action == "pr_approve":
                await self.github.approve_pr(ref)
                msg = f"Approved {ref.key}."
            elif action == "pr_close":
                await self.github.close_pr(ref)
                row.state = "closed"
                msg = f"Closed {ref.key}."
            else:
                return
            await self.db.upsert_pr(row)
            if interaction.message:
                await interaction.message.edit(view=None)  # drop stale buttons
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:  # noqa: BLE001 — show GitHub's reason
            await interaction.followup.send(f"`{e}`", ephemeral=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    if not settings.discord_token:
        raise SystemExit("DISCORD_TOKEN is not set (see .env.example)")
    if not settings.devin_api_key or not settings.devin_org_id:
        raise SystemExit("DEVIN_API_KEY / DEVIN_ORG_ID are not set (see .env.example)")
    if not settings.allowed_user_id_set:
        raise SystemExit("ALLOWED_USER_IDS is empty — refusing to start unlocked")
    DevinMobileBot(settings).run(settings.discord_token)


if __name__ == "__main__":
    main()

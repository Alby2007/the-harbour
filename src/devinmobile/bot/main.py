import asyncio
import logging

import discord
from discord import app_commands

from ..acp_bridge import AcpBridge
from ..config import Settings
from ..db import Database
from ..devin_client import DevinClient
from ..embeds import status_embed
from ..github_client import GithubClient, PullRef
from ..relay import Relay
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
        self.github: GithubClient | None
        self.relay: Relay
        self._relay_task: asyncio.Task | None = None
        self._webhook: WebhookServer | None = None

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
        if self.db:
            await self.db.close()
        if self.devin:
            await self.devin.aclose()
        if self.github:
            await self.github.aclose()
        if self._webhook:
            await self._webhook.stop()
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
        try:
            session = await self.devin.send_message(
                binding.session_id,
                message.content or "(attachment)",
                attachment_urls=[a.url for a in message.attachments] or None,
                message_as_user_id=self.settings.create_as_user_id,
            )
            binding.status = session.status
            binding.status_detail = session.status_detail
            binding.active = True
            await self.db.upsert_binding(binding)
            await message.add_reaction("\u2705")
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
            session = await self.devin.get_session(session_id)
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
            await self.devin.send_message(
                session_id,
                "Approved — please proceed.",
                message_as_user_id=self.settings.create_as_user_id,
            )
            await interaction.followup.send("Sent an approval to the session.", ephemeral=True)

    async def _handle_pr_action(
        self,
        interaction: discord.Interaction,
        action: str,
        session_id: str,
        pr_number: str | None,
    ) -> None:
        if self.github is None or not pr_number:
            await interaction.response.send_message(
                "GitHub App isn't configured (GITHUB_APP_*).", ephemeral=True
            )
            return
        row = await self.db.get_pr_by_number(session_id, int(pr_number))
        if row is None:
            await interaction.response.send_message(
                f"No PR #{pr_number} tracked for this session.", ephemeral=True
            )
            return
        ref = PullRef(owner=row.owner, repo=row.repo, number=row.number)

        if action == "pr_merge":
            # first click just asks for confirmation — the real merge happens
            # on pr_merge_go below
            await interaction.response.send_message(
                f"Merge **{row.pr_title or row.pr_url}** "
                f"({self.settings.github_merge_method})?",
                view=MergeConfirmView(session_id, row.number, self.handle_component),
                ephemeral=True,
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

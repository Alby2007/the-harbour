import asyncio
import logging

import discord
from discord import app_commands

from ..config import Settings
from ..db import Database
from ..devin_client import DevinClient
from ..embeds import status_embed
from ..relay import Relay
from ..views import dispatch
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
        self.relay: Relay
        self._relay_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        self.db = await Database.connect(self.settings.db_path)
        self.devin = DevinClient(
            self.settings.devin_api_key,
            self.settings.devin_org_id,
            self.settings.devin_base_url,
        )
        self.relay = Relay(self, self.devin, self.db, self.settings)
        register_commands(self)
        if self.settings.command_guild_id:
            guild = discord.Object(id=self.settings.command_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        await self.tree.sync()  # global: makes commands usable in DMs
        self._relay_task = asyncio.create_task(self.relay.run_forever())

    async def close(self) -> None:
        if self._relay_task:
            self._relay_task.cancel()
        if self.db:
            await self.db.close()
        if self.devin:
            await self.devin.aclose()
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
        self, interaction: discord.Interaction, action: str, session_id: str
    ) -> None:
        if interaction.user.id not in self.settings.allowed_user_id_set:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "This bot is locked to its owner.", ephemeral=True
                )
            return
        binding = await self.db.get_binding(session_id)
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

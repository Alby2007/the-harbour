import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from ..db import Binding
from ..devin_client import SESSION_TAG
from ..embeds import status_embed
from ..views import SessionView

if TYPE_CHECKING:
    from .main import DevinMobileBot

log = logging.getLogger(__name__)

DEVIN_MODES = ["lite", "normal", "fast", "ultra", "fusion"]

NOT_ALLOWED = "This bot is locked to its owner."


def register_commands(bot: "DevinMobileBot") -> None:
    tree = bot.tree

    def _allowed(interaction: discord.Interaction) -> bool:
        return interaction.user.id in bot.settings.allowed_user_id_set

    async def _hub() -> discord.TextChannel | None:
        if bot.settings.hub_channel_id is None:
            return None
        chan = bot.get_channel(bot.settings.hub_channel_id) or await bot.fetch_channel(
            bot.settings.hub_channel_id
        )
        return chan if isinstance(chan, discord.TextChannel) else None

    @tree.command(name="devin", description="Start a Devin Cloud session")
    @app_commands.describe(
        prompt="What Devin should do",
        repo="org/repo (comma-separate for several)",
        mode="Agent mode (default from env)",
        title="Thread/session title",
    )
    @app_commands.choices(mode=[app_commands.Choice(name=m, value=m) for m in DEVIN_MODES])
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def devin_cmd(
        interaction: discord.Interaction,
        prompt: str,
        repo: str | None = None,
        mode: str | None = None,
        title: str | None = None,
    ) -> None:
        if not _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        hub = await _hub()
        if hub is None:
            await interaction.response.send_message(
                "HUB_CHANNEL_ID is not configured or isn't a text channel.", ephemeral=True
            )
            return
        await interaction.response.defer()
        try:
            session = await bot.devin.create_session(
                prompt=prompt,
                repos=[r.strip() for r in repo.split(",") if r.strip()] if repo else None,
                devin_mode=mode or bot.settings.devin_mode,
                title=title,
                tags=[SESSION_TAG],
                max_acu_limit=bot.settings.max_acu_limit,
                create_as_user_id=bot.settings.create_as_user_id,
            )
        except Exception as e:
            await interaction.followup.send(f"Session create failed: `{e}`", ephemeral=True)
            return

        thread_name = (title or prompt)[:90] or session.session_id
        thread = await hub.create_thread(
            name=thread_name, type=discord.ChannelType.public_thread
        )
        try:
            await thread.join()
        except discord.HTTPException:
            pass
        anchor = await thread.send(
            embed=status_embed(session, fallback_title=title),
            view=SessionView(session.session_id, session.url, bot.handle_component),
        )
        await bot.db.upsert_binding(
            Binding(
                session_id=session.session_id,
                thread_id=thread.id,
                channel_id=hub.id,
                anchor_msg_id=anchor.id,
                title=title,
                url=session.url,
                status=session.status,
                status_detail=session.status_detail,
            )
        )
        await interaction.followup.send(f"Session started → {thread.mention}")

    @tree.command(name="sessions", description="List sessions started through this bot")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def sessions_cmd(interaction: discord.Interaction) -> None:
        if not _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        bindings = await bot.db.all_bindings(limit=10)
        if not bindings:
            await interaction.response.send_message("No sessions yet.", ephemeral=True)
            return
        embed = discord.Embed(title="Devin sessions")
        for b in bindings:
            status = b.status or "?"
            if b.status_detail:
                status += f" — {b.status_detail}"
            embed.add_field(
                name=(b.title or b.session_id)[:100],
                value=f"`{status}` · <#{b.thread_id}>",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="devin-status", description="Refresh a session's status")
    @app_commands.describe(session="Session id (default: most recent)")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def devin_status_cmd(
        interaction: discord.Interaction, session: str | None = None
    ) -> None:
        if not _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        binding = None
        if session:
            binding = await bot.db.get_binding(session)
        else:
            recent = await bot.db.all_bindings(limit=1)
            binding = recent[0] if recent else None
        if binding is None:
            await interaction.response.send_message("No such session.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        sess = await bot.devin.get_session(binding.session_id)
        await interaction.followup.send(
            embed=status_embed(sess, fallback_title=binding.title), ephemeral=True
        )

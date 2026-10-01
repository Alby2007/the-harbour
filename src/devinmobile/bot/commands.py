import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from ..acp_bridge import MODEL_ALIASES, BridgeError, ConfigOption, resolve_option
from ..db import Binding
from ..devin_client import SESSION_TAG
from ..embeds import status_embed
from ..github_client import parse_issue_ref
from ..models import Session
from ..views import SessionView

if TYPE_CHECKING:
    from .main import DevinMobileBot

log = logging.getLogger(__name__)

DEVIN_MODES = ["lite", "normal", "fast", "ultra", "fusion"]

NOT_ALLOWED = "This bot is locked to its owner."


async def model_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    cur = current.lower()
    return [
        app_commands.Choice(name=a, value=a) for a in MODEL_ALIASES if cur in a
    ][:25]


async def repo_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest canonical `owner/repo` values from the bridge's live catalog.

    Autocompletes the token after the last comma so multi-repo input works.
    Falls back to no suggestions (free text) when the bridge is unreachable —
    the bridge itself still canonicalizes/validates whatever is typed.
    """
    bridge = getattr(interaction.client, "bridge", None)
    if bridge is None or not bridge.available:
        return []
    try:
        options = (await bridge.catalog()).get("repos", {}).get("options") or []
    except Exception:  # noqa: BLE001 — autocomplete must never fail the command
        return []
    head, sep, tail = current.rpartition(",")
    cur = tail.strip().lower()
    prefix = head + sep if sep else ""
    out: list[app_commands.Choice[str]] = []
    for o in options:
        value, name = o.get("value", ""), o.get("name", "")
        if value and (not cur or cur in value.lower() or cur in name.lower()):
            out.append(app_commands.Choice(name=value, value=prefix + value))
    return out[:25]


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

    async def _wait_for_v3(session_id: str) -> Session:
        """Bridge sessions materialize for the v3 API once the first prompt
        lands — poll briefly until get_session succeeds."""
        last: Exception | None = None
        for _ in range(8):
            try:
                return await bot.devin.get_session(session_id)
            except Exception as e:  # noqa: BLE001 — any HTTP error just means "not yet"
                last = e
                await asyncio.sleep(2.5)
        raise BridgeError(f"session {session_id} not visible to the API yet") from last

    @tree.command(name="devin", description="Start a Devin Cloud session")
    @app_commands.describe(
        prompt="What Devin should do",
        model="Model picker (e.g. swe2-max, opus, fusion) — overrides mode",
        repo="org/repo (comma-separate for several)",
        issue="GitHub issue to attach as context (URL, owner/repo#n, or #n)",
        branch="Base branch to work from (added to the prompt)",
        mode="Agent mode (default from env)",
        title="Thread/session title",
    )
    @app_commands.choices(mode=[app_commands.Choice(name=m, value=m) for m in DEVIN_MODES])
    @app_commands.autocomplete(model=model_autocomplete, repo=repo_autocomplete)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def devin_cmd(
        interaction: discord.Interaction,
        prompt: str,
        model: str | None = None,
        repo: str | None = None,
        issue: str | None = None,
        branch: str | None = None,
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
        repos = [r.strip() for r in repo.split(",") if r.strip()] if repo else None
        if issue and not bot.settings.github_enabled:
            await interaction.response.send_message(
                "`issue:` needs the GitHub App configured "
                "(GITHUB_APP_ID / key path / installation id).", ephemeral=True
            )
            return
        await interaction.response.defer()

        if repos and bot.bridge.available:
            # Canonicalize against the live catalog even on the v3 path —
            # v3 silently drops repo values it doesn't recognize, same trap
            # as the bridge. Bridge off → pass raw (nothing to check against).
            # (Post-defer: a cold catalog() call can exceed the 3s window.)
            try:
                cat = await bot.bridge.catalog()
                repo_opts = [
                    ConfigOption(name=o.get("name", ""), value=o.get("value", ""))
                    for o in (cat.get("repos", {}).get("options") or [])
                ]
                if repo_opts:
                    repos = [
                        resolve_option(r, repo_opts, what="repo").value
                        for r in repos
                    ]
            except BridgeError as e:
                await interaction.followup.send(f"`{e}`", ephemeral=True)
                return
            except Exception:  # noqa: BLE001 — catalog fetch failed; pass raw
                pass

        if issue:
            default_repo = repos[0] if repos else None
            ref = parse_issue_ref(issue, default_repo)
            if ref is None:
                await interaction.followup.send(
                    f"Couldn't parse issue {issue!r} — use a URL, `owner/repo#n`, "
                    "or `#n` together with `repo:`.", ephemeral=True
                )
                return
            try:
                assert bot.github is not None  # gated by github_enabled above
                data = await bot.github.get_issue(*ref)
                o, r, n = ref
                body = (data.get("body") or "")[:4000]
                prompt += (
                    f"\n\n---\nGitHub issue https://github.com/{o}/{r}/issues/{n}\n"
                    f"**{data.get('title', '')}**\n\n{body}"
                )
                title = title or data.get("title") or None
            except Exception as e:  # noqa: BLE001 — surface GitHub's message
                await interaction.followup.send(
                    f"Issue fetch failed: `{e}`", ephemeral=True
                )
                return
        if branch:
            prompt += f"\n\nBase your work on branch `{branch}` (checkout from origin/{branch})."

        model_label: str | None = None
        # model beats mode; the env default only applies when neither is given
        effective_model = model or (bot.settings.default_model if mode is None else None)
        if effective_model:
            # Model selection needs the ACP bridge (CLI credentials); v3 only
            # exposes devin_mode. The bridge session joins the normal pipeline
            # once its first prompt lands.
            if not bot.bridge.available:
                await interaction.followup.send(
                    "Model selection needs `devin auth login` on the bot host "
                    "(no CLI credentials found). Run without `model:` or log in first.",
                    ephemeral=True,
                )
                return
            try:
                bs = await bot.bridge.create_cloud_session(
                    prompt, model=effective_model, repos=repos
                )
                session = await _wait_for_v3(bs.session_id)
                model_label = bs.model_label
            except BridgeError as e:
                await interaction.followup.send(f"Bridge create failed: `{e}`", ephemeral=True)
                return
            except Exception as e:
                await interaction.followup.send(
                    f"Session create failed: `{e}`", ephemeral=True
                )
                return
        else:
            try:
                session = await bot.devin.create_session(
                    prompt=prompt,
                    repos=repos,
                    devin_mode=mode or bot.settings.devin_mode,
                    title=title,
                    tags=[SESSION_TAG],
                    max_acu_limit=bot.settings.max_acu_limit,
                    create_as_user_id=bot.settings.create_as_user_id,
                )
            except Exception as e:
                await interaction.followup.send(
                    f"Session create failed: `{e}`", ephemeral=True
                )
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
            embed=status_embed(session, fallback_title=title, model=model_label),
            view=SessionView(session.session_id, session.url, bot.handle_component),
        )
        binding = Binding(
            session_id=session.session_id,
            thread_id=thread.id,
            channel_id=hub.id,
            anchor_msg_id=anchor.id,
            title=title,
            url=session.url,
            status=session.status,
            status_detail=session.status_detail,
            model=model_label,
        )
        await bot.db.upsert_binding(binding)
        await interaction.followup.send(f"Session started → {thread.mention}")
        # Devin's first ack lands within seconds — poll immediately instead
        # of waiting for the first scheduled tick.
        bot.relay.request_poll(binding)

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

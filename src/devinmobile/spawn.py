"""Shared session-spawn pipeline — one path for /devin, /devin-all,
/schedule, and the GitHub issue-label trigger.

Everything after prompt assembly lives here so every spawn path gets repo
canonicalization, bridge-vs-v3 routing, thread creation, binding
persistence, and the immediate first poll for free.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord

from .acp_bridge import BridgeError, ConfigOption, resolve_option
from .db import Binding
from .devin_client import SESSION_TAG
from .embeds import status_embed
from .models import Session
from .views import SessionView

if TYPE_CHECKING:
    from .bot.main import DevinMobileBot

log = logging.getLogger(__name__)


class SpawnError(RuntimeError):
    """User-facing spawn failure (no hub, bad repo, bridge/API error)."""


async def _wait_for_v3(bot: DevinMobileBot, session_id: str) -> Session:
    """Bridge sessions materialize for the v3 API once the first prompt
    lands — poll briefly until get_session succeeds."""
    last: Exception | None = None
    for _ in range(8):
        try:
            return await bot.devin.get_session(session_id)
        except Exception as e:  # noqa: BLE001 — any HTTP error just means "not yet"
            last = e
            await asyncio.sleep(2.5)
    raise SpawnError(f"session {session_id} not visible to the API yet") from last


async def spawn_session(
    bot: DevinMobileBot,
    *,
    prompt: str,
    repos: list[str] | None = None,
    model: str | None = None,
    mode: str | None = None,
    title: str | None = None,
    budget: float | None = None,
    continued_from: str = "",
    review_of: str = "",
) -> tuple[Session, discord.Thread]:
    """Create a Devin session + its Discord thread + binding.

    Raises SpawnError for anything the caller should surface to the user.
    """
    hub_id = bot.settings.hub_channel_id
    if hub_id is None:
        raise SpawnError("HUB_CHANNEL_ID is not configured.")
    chan = bot.get_channel(hub_id) or await bot.fetch_channel(hub_id)
    if not isinstance(chan, discord.TextChannel):
        raise SpawnError("HUB_CHANNEL_ID is not configured or isn't a text channel.")

    if repos and bot.bridge.available:
        # Canonicalize against the live catalog even on the v3 path — v3
        # silently drops repo values it doesn't recognize, same trap as the
        # bridge. Bridge off → pass raw (nothing to check against).
        try:
            cat = await bot.bridge.catalog()
            repo_opts = [
                ConfigOption(name=o.get("name", ""), value=o.get("value", ""))
                for o in (cat.get("repos", {}).get("options") or [])
            ]
            if repo_opts:
                repos = [resolve_option(r, repo_opts, what="repo").value for r in repos]
        except BridgeError as e:
            raise SpawnError(str(e)) from e
        except Exception:  # noqa: BLE001 — catalog fetch failed; pass raw
            pass

    model_label: str | None = None
    # model beats mode; the env default only applies when neither is given
    effective_model = model or (bot.settings.default_model if mode is None else None)
    if effective_model:
        # Model selection needs the ACP bridge (CLI credentials); v3 only
        # exposes devin_mode.
        if not bot.bridge.available:
            raise SpawnError(
                "Model selection needs `devin auth login` on the bot host "
                "(no CLI credentials found). Run without `model:` or log in first."
            )
        try:
            bs = await bot.bridge.create_cloud_session(
                prompt, model=effective_model, repos=repos
            )
            session = await _wait_for_v3(bot, bs.session_id)
            model_label = bs.model_label
        except BridgeError as e:
            raise SpawnError(f"Bridge create failed: `{e}`") from e
        except Exception as e:
            raise SpawnError(f"Session create failed: `{e}`") from e
    else:
        try:
            session = await bot.devin.create_session(
                prompt=prompt,
                repos=repos,
                devin_mode=mode or bot.settings.devin_mode,
                title=title,
                tags=[SESSION_TAG],
                max_acu_limit=(
                    int(budget) if budget else bot.settings.max_acu_limit
                ),
                create_as_user_id=bot.settings.create_as_user_id,
            )
        except Exception as e:
            raise SpawnError(f"Session create failed: `{e}`") from e

    thread_name = (title or prompt)[:90] or session.session_id
    thread = await chan.create_thread(
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
        channel_id=chan.id,
        anchor_msg_id=anchor.id,
        title=title,
        url=session.url,
        status=session.status,
        status_detail=session.status_detail,
        model=model_label,
        repos=",".join(repos or []),
        continued_from=continued_from,
        max_acu=budget,
        review_of=review_of,
        last_activity_at=int(time.time()),
    )
    await bot.db.upsert_binding(binding)
    # Cover the create→first-ack gap (measured ~5s) with the thinking
    # placeholder; it morphs into the live Working display on first tool call.
    await bot.relay.progress.thinking(binding)
    # Devin's first ack lands within seconds — poll immediately instead of
    # waiting for the first scheduled tick.
    bot.relay.request_poll(binding)
    return session, thread

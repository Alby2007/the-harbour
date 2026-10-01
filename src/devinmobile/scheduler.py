"""Recurring-task runner for /schedule rows.

One task wakes every minute, fires `spawn_session` for each due row, and
slides `next_run_at` forward from fire-time — a long bot downtime skips
the backlog rather than firing a catch-up storm.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord

from .digest import build_digest
from .spawn import SpawnError, spawn_session

if TYPE_CHECKING:
    from .bot.main import DevinMobileBot

log = logging.getLogger(__name__)

TICK_SECONDS = 60


class Scheduler:
    def __init__(self, bot: DevinMobileBot) -> None:
        self.bot = bot
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self._fire_due()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def _fire_due(self) -> None:
        now = int(time.time())
        for s in await self.bot.db.due_schedules(now):
            if s.kind == "digest":
                await self._fire_digest(s, now)
                continue
            try:
                session, thread = await spawn_session(
                    self.bot,
                    prompt=s.prompt,
                    repos=s.repos or None,
                    model=s.model,
                    title=f"[scheduled] {s.prompt[:60]}",
                )
                await thread.send(
                    f"Spawned by schedule #{s.id} (every {s.interval_seconds // 60}m)."
                )
                await self.bot.db.schedule_ran(s.id, session.session_id, now)
            except SpawnError as e:
                log.warning("schedule #%d spawn failed: %s", s.id, e)
                # still advance — a broken schedule shouldn't retry every tick
                await self.bot.db.schedule_ran(s.id, "", now)
            except Exception:
                log.exception("schedule #%d spawn crashed", s.id)

    async def _fire_digest(self, s, now: int) -> None:
        """digest-kind row → post the window rollup to the hub channel
        instead of spawning. Same slide-forward semantics as spawn —
        including on failure, so a dead hub channel can't retry-storm
        every tick."""
        try:
            since = now - s.interval_seconds
            bindings = await self.bot.db.bindings_since(since)
            hub_id = self.bot.settings.hub_channel_id
            chan = (
                self.bot.get_channel(hub_id)
                or await self.bot.fetch_channel(hub_id)
            ) if hub_id is not None else None
            if isinstance(chan, discord.abc.Messageable):
                await chan.send(embed=build_digest(bindings, since))
        except Exception:
            log.exception("schedule #%d digest crashed", s.id)
        finally:
            await self.bot.db.schedule_ran(s.id, "", now)

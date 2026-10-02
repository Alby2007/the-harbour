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
from .inbox import build_inbox_embed
from .monitors import run_check
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
            if s.kind == "monitor":
                await self._fire_monitor(s, now)
                continue
            if s.kind == "inbox":
                await self._fire_inbox(s, now)
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
            if not await self._post_hub(embed=build_digest(bindings, since)):
                # silent no-op otherwise: a digest schedule with no hub
                # would fire forever with zero trace of why nothing posts
                log.warning(
                    "digest schedule #%d: hub channel unresolvable "
                    "(HUB_CHANNEL_ID unset or wrong?)", s.id
                )
        except Exception:
            log.exception("schedule #%d digest crashed", s.id)
        finally:
            await self.bot.db.schedule_ran(s.id, "", now)

    async def _fire_monitor(self, s, now: int) -> None:
        """monitor-kind row — cheap check each tick, spawn only on a red
        edge (or a still-red cooldown expiry), never while the previous
        fix session is still running. `unknown` (GitHub/API outage,
        malformed watch) is never red."""
        # schedule_ran writes last_session_id — keep the prior pointer on
        # non-spawn paths; it's the still-running dedup key.
        fired_id = s.last_session_id or ""
        try:
            result = await run_check(s.watch, s.expect, self.bot.github)
            if result.state == "unknown":
                log.warning(
                    "monitor #%d %s unknown: %s", s.id, s.watch, result.detail
                )
            elif result.state == "ok":
                # state flip BEFORE the post — a dead hub channel can't
                # leave the monitor stuck "red" and refiring
                await self.bot.db.update_watch(s.id, "green")
                if s.watch_state == "red":
                    await self._post_hub(
                        f"✅ Monitor recovered: `{s.watch}` — {result.detail}"
                    )
            else:  # red
                edge = s.watch_state != "red"
                cooled = (
                    s.last_fired_at is None
                    or now - s.last_fired_at >= s.cooldown_seconds
                )
                still_running = False
                if s.last_session_id:
                    prev = await self.bot.db.get_binding(s.last_session_id)
                    still_running = bool(prev and prev.active)
                if (edge or cooled) and not still_running:
                    session, thread = await spawn_session(
                        self.bot,
                        prompt=(
                            f"{s.prompt}\n\n---\nMonitor tripped: {s.watch} "
                            f"→ {result.detail} at "
                            f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))}"
                        ),
                        repos=s.repos or None,
                        model=s.model,
                        title=f"[monitor] {s.watch[:60]}",
                    )
                    # bookkeeping BEFORE the provenance post — a failed
                    # send mustn't hide the spawn from the dedup key or
                    # re-edge the monitor every tick
                    fired_id = session.session_id
                    await self.bot.db.update_watch(s.id, "red", now)
                    try:
                        await thread.send(
                            f"Spawned by monitor schedule #{s.id} "
                            f"(`{s.watch}` → {result.detail})."
                        )
                    except Exception:
                        log.warning(
                            "monitor #%d provenance post failed", s.id
                        )
                else:
                    # still red but suppressed — persist state only
                    log.info(
                        "monitor #%d %s red — suppressed (edge=%s cooled=%s "
                        "running=%s)",
                        s.id, s.watch, edge, cooled, still_running,
                    )
                    await self.bot.db.update_watch(s.id, "red")
        except SpawnError as e:
            log.warning("monitor #%d spawn failed: %s", s.id, e)
            # record the attempt as red+fired — otherwise the edge stays
            # fresh and a permanently-broken spawn path retries every
            # interval, ignoring cooldown entirely
            await self.bot.db.update_watch(s.id, "red", now)
        except Exception:
            log.exception("monitor #%d crashed", s.id)
        finally:
            await self.bot.db.schedule_ran(s.id, fired_id, now)

    async def _fire_inbox(self, s, now: int) -> None:
        """inbox-kind row → the prospective triage card, posted to hub on
        a daily cadence. Same slide-forward semantics."""
        try:
            embed = await build_inbox_embed(self.bot.db)
            if not await self._post_hub(embed=embed):
                log.warning(
                    "inbox schedule #%d: hub channel unresolvable "
                    "(HUB_CHANNEL_ID unset or wrong?)", s.id
                )
        except Exception:
            log.exception("inbox schedule #%d crashed", s.id)
        finally:
            await self.bot.db.schedule_ran(s.id, "", now)

    async def _post_hub(self, text: str = "", **kw) -> bool:
        hub_id = self.bot.settings.hub_channel_id
        chan = (
            self.bot.get_channel(hub_id)
            or await self.bot.fetch_channel(hub_id)
        ) if hub_id is not None else None
        if not isinstance(chan, discord.abc.Messageable):
            return False
        await chan.send(text or discord.utils.MISSING, **kw)
        return True

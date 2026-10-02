"""Inbox — the prospective counterpart to /digest's retrospective: one
card listing everything awaiting a human tap, built from local db state
(no API calls). Sections are urgency-ordered; rows link into the thread
where the action affordances already live.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import discord

from .db import Binding, PrRow, ScheduleRow

if TYPE_CHECKING:
    from .db import Database

_WAITING = {"waiting_for_user", "waiting_for_approval"}


def _line(b: Binding, extra: str = "") -> str:
    title = (b.title or b.session_id[:12])[:80]
    return f"{title} · <#{b.thread_id}>{extra}"


def build_inbox(
    waiting: list[Binding],
    errored: list[Binding],
    pending_chains: list[Binding],
    red_monitors: list[ScheduleRow],
    open_prs: list[PrRow],
    running_n: int,
    suggestions: list[str] | None = None,
) -> discord.Embed:
    embed = discord.Embed(title="Devin inbox — what needs a tap")
    total = (
        len(waiting) + len(errored) + len(pending_chains)
        + len(red_monitors) + len(open_prs)
    )
    if not total:
        embed.description = "Inbox zero — nothing waiting on you."
        if suggestions:
            embed.description += (
                "\nIdle repos worth a look: "
                + ", ".join(f"`{r}`" for r in suggestions[:2])
            )
        if running_n:
            embed.set_footer(text=f"{running_n} running")
        return embed

    if waiting:
        embed.add_field(
            name=f"Waiting on you ({len(waiting)})",
            value="\n".join(
                _line(
                    b,
                    " — " + (b.last_msg or "")[:80]
                    if (b.last_msg or "").rstrip().endswith("?")
                    else "",
                )
                for b in waiting[:10]
            )[:1024],
            inline=False,
        )
    if errored:
        embed.add_field(
            name=f"Errored ({len(errored)})",
            value="\n".join(_line(b) for b in errored[:10])[:1024],
            inline=False,
        )
    if pending_chains:
        embed.add_field(
            name=f"Chains awaiting Continue → ({len(pending_chains)})",
            value="\n".join(_line(b) for b in pending_chains[:10])[:1024],
            inline=False,
        )
    if red_monitors:
        embed.add_field(
            name=f"Monitors red ({len(red_monitors)})",
            value="\n".join(f"`{s.watch[:90]}`" for s in red_monitors[:10])[
                :1024
            ],
            inline=False,
        )
    if open_prs:
        embed.add_field(
            name=f"Open PRs ({len(open_prs)})",
            value="\n".join(
                f"{p.owner}/{p.repo}#{p.number} — {p.checks_state or 'none'}"
                + (" · armed" if p.auto_merge else "")
                for p in open_prs[:10]
            )[:1024],
            inline=False,
        )
    embed.set_footer(
        text=f"{total} waiting · {running_n} running"
    )
    return embed


async def build_inbox_embed(db: Database) -> discord.Embed:
    """Gather the rows and build the card — shared by /inbox and
    kind='inbox' schedule fires."""
    rows = await db.inbox_bindings()
    waiting = [
        b for b in rows if b.active and (b.status_detail or "") in _WAITING
    ]
    errored_all = [b for b in rows if b.status == "error"]
    if errored_all:
        # an errored session that already produced a continuation child
        # isn't inbox work — the child is the live thread
        parents = await db.continued_parents()
        errored = [b for b in errored_all if b.session_id not in parents]
    else:
        errored = []
    pending_chains = [
        b for b in rows
        if b.chain and b.chain.get("pending") is not None
    ]
    schedules = await db.all_schedules()
    red_monitors = [
        s for s in schedules
        if s.kind == "monitor" and s.watch_state == "red"
    ]
    open_prs = await db.open_prs()
    running_n = sum(1 for b in rows if b.active)
    suggestions = (
        [] if (waiting or errored or pending_chains or red_monitors or open_prs)
        else await db.idle_repos(int(time.time()) - 7 * 86400, limit=2)
    )
    return build_inbox(
        waiting, errored, pending_chains, red_monitors, open_prs,
        running_n, suggestions,
    )

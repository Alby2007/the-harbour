"""Nightly-standup rollup — `build_digest` renders a window's sessions into
one embed (Completed / Still running / Errored), read entirely from
`bindings.summary` + status. No API calls: summaries are persisted at
completion time, so a digest is one local SQL read.
"""

from __future__ import annotations

import discord

from .db import Binding

_DONE = {"exit"}     # classify_transition emits "complete" on exit
_ERRORED = {"error"}
_SUSPENDED = {"suspended"}  # parked — not running, not a failure


def _row(binding: Binding) -> str:
    # bound each piece — one long title shouldn't eat the field's 1024
    title = (binding.title or binding.session_id[:12])[:80]
    desc = f" — {binding.summary[:120]}" if binding.summary else ""
    acu = f" · {binding.acus:g} ACU" if binding.acus else ""
    return f"{title}{desc} · <#{binding.thread_id}>{acu}"


def build_digest(bindings: list[Binding], since: int) -> discord.Embed:
    # <t:> timestamps render in descriptions/fields but NOT titles/footers
    embed = discord.Embed(
        title="Devin digest",
        description=f"Sessions touched since <t:{since}:R>",
    )
    if not bindings:
        embed.description = f"Quiet — nothing touched since <t:{since}:R>."
        return embed
    sections = (
        ("Completed", _DONE), ("Errored", _ERRORED),
        ("Suspended", _SUSPENDED),
        ("In flight", None),  # None = everything else
    )
    total = 0.0
    for label, statuses in sections:
        group = [
            b for b in bindings
            if (b.status in statuses
                if statuses else b.status not in _DONE | _ERRORED | _SUSPENDED)
        ]
        if not group:
            continue
        total += sum(b.acus for b in group)
        embed.add_field(
            name=label,
            value="\n".join(_row(b) for b in group)[:1024],
            inline=False,
        )
    embed.set_footer(text=f"{len(bindings)} sessions · {total:g} ACU in window")
    return embed

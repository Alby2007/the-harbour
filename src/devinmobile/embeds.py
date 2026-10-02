import discord

from .models import Session

STATUS_COLORS = {
    "new": 0x8A8A8A,
    "claimed": 0x8A8A8A,
    "resuming": 0xE6A23C,
    "running": 0x2B6CB0,
    "exit": 0x2F9E44,
    "error": 0xC92A2A,
    "suspended": 0xE8590C,
}

_FIELD_LIMIT = 1000


def mention_for(spawned_by: str, allowed: frozenset[int]) -> str:
    """Mention string for a notification — the session owner when the
    spawner was a Discord user; infra/marker spawnings ('github',
    'intake', a token-map name, '' legacy) join the whole allowlist, the
    right call for team events nobody in particular owns.

    A digit string only routes as an owner when it's an allowlisted id —
    `/task`'s caller-supplied `by:` can't redirect pings at a stranger."""
    if spawned_by.isdigit() and int(spawned_by) in allowed:
        return f"<@{spawned_by}>"
    return " ".join(f"<@{u}>" for u in allowed)


def spawned_by_label(spawned_by: str) -> str:
    """Display form of a spawned_by value — a ping for snowflakes, the raw
    marker text otherwise (markers don't ping in embed field values anyway,
    but rows reuse this)."""
    return f"<@{spawned_by}>" if spawned_by.isdigit() else spawned_by


def status_embed(
    session: Session,
    *,
    fallback_title: str | None = None,
    model: str | None = None,
    spawned_by: str = "",
) -> discord.Embed:
    # binding.title (the /rename or spawn-time name) beats the session's
    # auto-title — user intent wins, consistent with spawn-time naming
    title = fallback_title or session.title or session.session_id
    embed = discord.Embed(
        title=title[:100],
        url=session.url,
        color=STATUS_COLORS.get(session.status, 0x8A8A8A),
    )
    detail = f" — `{session.status_detail}`" if session.status_detail else ""
    embed.add_field(name="Status", value=f"`{session.status}`{detail}", inline=False)
    bits = []
    if model:
        bits.append(f"model `{model}`")
    if session.devin_mode:
        bits.append(f"mode `{session.devin_mode}`")
    bits.append(f"{session.acus_consumed:g} ACUs")
    embed.add_field(name="Session", value=" · ".join(bits), inline=False)
    if spawned_by:
        embed.add_field(
            name="By", value=spawned_by_label(spawned_by), inline=True
        )
    for pr in session.pull_requests[:5]:
        embed.add_field(name=f"PR ({pr.pr_state or '?'})", value=pr.pr_url, inline=False)
    embed.set_footer(text=session.session_id)
    return embed


CHECKS_ICONS = {"success": "✅", "failure": "❌", "pending": "🟡", "none": "—"}
PR_STATE_ICONS = {"open": "🟢", "closed": "🔴", "merged": "🟣"}
PR_COLORS = {"open": 0x2F9E44, "closed": 0xC92A2A, "merged": 0x845EF7}


def pr_embed(
    *,
    owner: str,
    repo: str,
    number: int,
    pr_title: str | None,
    state: str | None,
    checks: str | None,
    url: str,
) -> discord.Embed:
    state = state or "open"
    embed = discord.Embed(
        title=f"PR #{number}: {pr_title or f'{owner}/{repo}'}"[:200],
        url=url,
        color=PR_COLORS.get(state, 0x8A8A8A),
    )
    ci = CHECKS_ICONS.get(checks or "none", "—")
    st = PR_STATE_ICONS.get(state, "⚪")
    embed.add_field(name="State", value=f"{st} {state} · CI {ci}", inline=True)
    return embed


def completion_embed(
    session: Session,
    *,
    fallback_title: str | None = None,
    model: str | None = None,
    spawned_by: str = "",
) -> discord.Embed:
    embed = status_embed(
        session, fallback_title=fallback_title, model=model,
        spawned_by=spawned_by,
    )
    out = session.structured_output or {}
    summary = out.get("summary")
    if summary:
        embed.description = str(summary)[:2000]
    files = out.get("files_changed")
    if isinstance(files, list) and files:
        shown = files[:15]
        text = "\n".join(f"`{f}`" for f in shown)
        if len(files) > len(shown):
            text += f"\n… and {len(files) - len(shown)} more"
        embed.add_field(name="Files changed", value=text[:_FIELD_LIMIT], inline=False)
    tests = out.get("tests_passed")
    if tests is not None:
        embed.add_field(name="Tests", value="passed" if tests else "FAILED", inline=True)
    notes = out.get("notes")
    if notes:
        embed.add_field(name="Notes", value=str(notes)[:_FIELD_LIMIT], inline=False)
    return embed

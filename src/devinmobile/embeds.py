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


def status_embed(session: Session, *, fallback_title: str | None = None) -> discord.Embed:
    title = session.title or fallback_title or session.session_id
    embed = discord.Embed(
        title=title[:100],
        url=session.url,
        color=STATUS_COLORS.get(session.status, 0x8A8A8A),
    )
    detail = f" — `{session.status_detail}`" if session.status_detail else ""
    embed.add_field(name="Status", value=f"`{session.status}`{detail}", inline=False)
    bits = []
    if session.devin_mode:
        bits.append(f"mode `{session.devin_mode}`")
    bits.append(f"{session.acus_consumed:g} ACUs")
    embed.add_field(name="Session", value=" · ".join(bits), inline=False)
    for pr in session.pull_requests[:5]:
        embed.add_field(name=f"PR ({pr.pr_state or '?'})", value=pr.pr_url, inline=False)
    embed.set_footer(text=session.session_id)
    return embed


def completion_embed(session: Session, *, fallback_title: str | None = None) -> discord.Embed:
    embed = status_embed(session, fallback_title=fallback_title)
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

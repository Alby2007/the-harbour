import logging
import re
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from ..acp_bridge import MODEL_ALIASES
from ..db import ScheduleRow
from ..embeds import status_embed
from ..github_client import parse_issue_ref
from ..spawn import SpawnError, spawn_session

if TYPE_CHECKING:
    from .main import DevinMobileBot

log = logging.getLogger(__name__)

DEVIN_MODES = ["lite", "normal", "fast", "ultra", "fusion"]

NOT_ALLOWED = "This bot is locked to its owner."

# /schedule every: values — 30m, 6h, 1d
EVERY_RE = re.compile(r"^(\d+)([mhd])$", re.IGNORECASE)
_EVERY_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def parse_every(text: str) -> int | None:
    """'30m'/'6h'/'1d' -> seconds, or None."""
    m = EVERY_RE.match(text.strip())
    if not m:
        return None
    return int(m.group(1)) * _EVERY_SECONDS[m.group(2).lower()]


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


def _split_repos(repo: str | None) -> list[str] | None:
    return [r.strip() for r in repo.split(",") if r.strip()] if repo else None


def register_commands(bot: "DevinMobileBot") -> None:
    tree = bot.tree

    def _allowed(interaction: discord.Interaction) -> bool:
        return interaction.user.id in bot.settings.allowed_user_id_set

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
        repos = _split_repos(repo)
        if issue and not bot.settings.github_enabled:
            await interaction.response.send_message(
                "`issue:` needs the GitHub App configured "
                "(GITHUB_APP_ID / key path / installation id).", ephemeral=True
            )
            return
        await interaction.response.defer()

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

        try:
            session, thread = await spawn_session(
                bot, prompt=prompt, repos=repos, model=model, mode=mode, title=title,
            )
        except SpawnError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        await interaction.followup.send(f"Session started → {thread.mention}")

    @tree.command(
        name="devin-all", description="Start one session per repo (fan-out)"
    )
    @app_commands.describe(
        prompt="What Devin should do in each repo",
        repos="Comma-separated owner/repo list (2+)",
        model="Model picker — overrides mode",
        mode="Agent mode (default from env)",
        title="Thread title prefix",
    )
    @app_commands.choices(mode=[app_commands.Choice(name=m, value=m) for m in DEVIN_MODES])
    @app_commands.autocomplete(model=model_autocomplete, repos=repo_autocomplete)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def devin_all_cmd(
        interaction: discord.Interaction,
        prompt: str,
        repos: str,
        model: str | None = None,
        mode: str | None = None,
        title: str | None = None,
    ) -> None:
        if not _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        repo_list = _split_repos(repos) or []
        if len(repo_list) < 2:
            await interaction.response.send_message(
                "Give at least two repos, comma-separated.", ephemeral=True
            )
            return
        await interaction.response.defer()
        spawned, failed = [], []
        for r in repo_list:
            try:
                _, thread = await spawn_session(
                    bot, prompt=prompt, repos=[r], model=model, mode=mode,
                    title=f"{title + ' · ' if title else ''}{r}",
                )
                spawned.append(f"{r} → {thread.mention}")
            except SpawnError as e:
                failed.append(f"{r}: {e}")
        text = "**Fan-out:**\n" + "\n".join(spawned)
        if failed:
            text += "\n\n**Failed:**\n" + "\n".join(f"`{f}`" for f in failed)
        await interaction.followup.send(text)

    @tree.command(name="schedule", description="Run a prompt on a recurring interval")
    @app_commands.describe(
        prompt="What Devin should do each run",
        every="Interval: 30m, 6h, 1d, …",
        repo="org/repo (comma-separate for several)",
        model="Model picker (optional)",
    )
    @app_commands.autocomplete(model=model_autocomplete, repo=repo_autocomplete)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def schedule_cmd(
        interaction: discord.Interaction,
        prompt: str,
        every: str,
        repo: str | None = None,
        model: str | None = None,
    ) -> None:
        if not _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        interval = parse_every(every)
        if interval is None or interval < 300:
            await interaction.response.send_message(
                "Interval must be like `30m`, `6h`, `1d` (min 5m).", ephemeral=True
            )
            return
        row = ScheduleRow(
            id=0,
            prompt=prompt,
            repos=_split_repos(repo) or [],
            model=model,
            interval_seconds=interval,
            next_run_at=int(time.time()) + interval,
        )
        row.id = await bot.db.add_schedule(row)
        await interaction.response.send_message(
            f"Scheduled #{row.id} — every {every}, first run in {every}.\n"
            f"`{prompt[:80]}` on {', '.join(row.repos) or 'default repos'}",
            ephemeral=True,
        )

    @tree.command(name="schedules", description="List recurring Devin tasks")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def schedules_cmd(interaction: discord.Interaction) -> None:
        if not _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        rows = await bot.db.all_schedules()
        if not rows:
            await interaction.response.send_message("No schedules.", ephemeral=True)
            return
        embed = discord.Embed(title="Recurring Devin tasks")
        now = int(time.time())
        for s in rows:
            state = "on" if s.enabled else "off"
            due = "due now" if s.next_run_at <= now else f"next <t:{s.next_run_at}:R>"
            embed.add_field(
                name=f"#{s.id} · every {s.interval_seconds // 60}m · {state}",
                value=(
                    f"`{s.prompt[:80]}`\n{', '.join(s.repos) or 'default repos'} · {due}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="unschedule", description="Delete a recurring task")
    @app_commands.describe(schedule_id="Schedule id from /schedules")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def unschedule_cmd(interaction: discord.Interaction, schedule_id: int) -> None:
        if not _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        if await bot.db.delete_schedule(schedule_id):
            await interaction.response.send_message(
                f"Deleted schedule #{schedule_id}.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"No schedule #{schedule_id}.", ephemeral=True
            )

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
            bits = [f"`{status}`"]
            if b.model:
                bits.append(f"`{b.model}`")
            if b.acus:
                bits.append(f"{b.acus:g} ACU")
            bits.append(f"<#{b.thread_id}>")
            embed.add_field(
                name=(b.title or b.session_id)[:100],
                value=" · ".join(bits),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="kill", description="Park a session (stop tracking it)")
    @app_commands.describe(session="Session id (default: this thread's session)")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def kill_cmd(
        interaction: discord.Interaction, session: str | None = None
    ) -> None:
        if not _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        binding = None
        if session:
            binding = await bot.db.get_binding(session)
        elif isinstance(interaction.channel, discord.Thread):
            binding = await bot.db.get_binding_by_thread(interaction.channel.id)
        if binding is None:
            await interaction.response.send_message(
                "No such session — run inside its thread or pass `session:`.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        # Best-effort terminate — v3 has no documented DELETE, so parking +
        # archiving is the contract; the API call just stops ACU burn sooner.
        try:
            await bot.devin.terminate_session(binding.session_id)
        except Exception:  # noqa: BLE001 — v3 may not support it; local park is enough
            pass
        binding.active = False
        await bot.db.upsert_binding(binding)
        thread = await interaction.client.fetch_channel(binding.thread_id)
        if isinstance(thread, discord.Thread):
            await thread.send("Session parked — no longer tracked.")
            try:
                await thread.edit(archived=True)
            except discord.HTTPException:
                pass
        await interaction.followup.send(
            f"Parked `{binding.session_id}` and archived its thread.", ephemeral=True
        )

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
            embed=status_embed(sess, fallback_title=binding.title),
            ephemeral=True,
        )

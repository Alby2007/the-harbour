import logging
import re
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from ..acp_bridge import MODEL_ALIASES
from ..chains import PLAYBOOKS, continued_chain, render_prompt
from ..db import Binding, ScheduleRow
from ..digest import build_digest
from ..embeds import spawned_by_label, status_embed
from ..github_client import parse_issue_ref
from ..inbox import build_inbox_embed
from ..monitors import parse_watch
from ..spawn import SpawnError, spawn_session

if TYPE_CHECKING:
    from .main import DevinMobileBot

log = logging.getLogger(__name__)

DEVIN_MODES = ["lite", "normal", "fast", "ultra", "fusion"]

NOT_ALLOWED = "This bot is locked to its owner."

# /schedule recipe: canned maintenance loops — one-tap instead of
# prompt-writing. Prompts are deliberately specific about output shape so
# the nightly run produces a reviewable artifact, not a vibes report.
RECIPES: dict[str, str] = {
    "dep-audit": (
        "Audit this repository's dependencies. List outdated packages and "
        "flag known-vulnerable versions (pip-audit / npm audit / cargo "
        "audit — whatever fits the stack). Open a PR upgrading the safe "
        "ones, and end with a summary of what you found and what changed."
    ),
    "test-coverage": (
        "Measure test coverage for this repository with the stack's "
        "tooling (pytest --cov, nyc, tarpaulin, …). Identify the "
        "least-covered critical modules, add tests for the top gaps, and "
        "open a PR with the new tests plus a coverage summary."
    ),
    "security-scan": (
        "Security-review this repository: run the available scanners "
        "(bandit / semgrep / trivy as appropriate), audit secrets handling "
        "and auth paths, and check dependencies for CVEs. Report findings "
        "by severity in your final message; fix trivial ones in a PR."
    ),
}

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


def user_role_ids(user) -> list[int]:
    """Snowflakes from `Member.roles`; a DM `User` (and `None`) carries
    none — DM access is always allowlist/db-only, no role shortcut."""
    return [r.id for r in getattr(user, "roles", ())]


async def _is_operator(bot: "DevinMobileBot", user_id: int, role_ids=()) -> bool:
    """The union that every gate checks: env allowlist ∪ REQUIRED_ROLE_ID
    (guild only) ∪ the runtime-allowed db table (/allow rows)."""
    if bot.settings.is_operator(user_id, role_ids):
        return True
    return await bot.db.is_allowed_user(user_id)


def _may_destroy(settings, actor_id: int, owner: str) -> bool:
    """Owner-or-admin gate for destructive ops on someone else's resource.

    TEAM_ADMIN_IDS unset → flat trust (the solo default — no gate at all).
    Marker/legacy owners ('', 'github', 'intake', token names) are shared
    infra — nobody's resource, so nobody gates them."""
    admins = settings.admin_user_id_set
    if not admins or not owner.isdigit() or owner == str(actor_id):
        return True
    return actor_id in admins


_OWNER_OR_ADMIN = "That belongs to someone else — owner or admin only."


def usage_summary(bindings: list[Binding], now: float) -> dict:
    """Aggregate per-session ACU burn for /usage.

    `acus` is cumulative per session with no time series, so day/week
    buckets group by session *start* (created_at) — "burn of sessions
    started in the window", not "burn in the window". Repo attribution
    uses the primary (first) repo only."""
    today0 = int(
        datetime.fromtimestamp(now).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
    )
    week0 = int((datetime.fromtimestamp(now) - timedelta(days=7)).timestamp())
    out: dict = {
        "today": 0.0, "week": 0.0, "total": 0.0, "count": len(bindings),
        "top": [], "by_repo": {}, "by_user": {},
    }
    for b in bindings:
        out["total"] += b.acus
        if b.created_at >= today0:
            out["today"] += b.acus
        if b.created_at >= week0:
            out["week"] += b.acus
        repo = (b.repos.split(",")[0].strip() if b.repos else "") or "(no repo)"
        agg = out["by_repo"].setdefault(repo, [0.0, 0])
        agg[0] += b.acus
        agg[1] += 1
        who = b.spawned_by or "unattributed"
        uagg = out["by_user"].setdefault(who, [0.0, 0])
        uagg[0] += b.acus
        uagg[1] += 1
    out["top"] = sorted(bindings, key=lambda b: b.acus, reverse=True)[:5]
    out["by_repo"] = dict(
        sorted(out["by_repo"].items(), key=lambda kv: kv[1][0], reverse=True)[:8]
    )
    out["by_user"] = dict(
        sorted(out["by_user"].items(), key=lambda kv: kv[1][0], reverse=True)[:8]
    )
    return out


def register_commands(bot: "DevinMobileBot") -> None:
    tree = bot.tree

    async def _allowed(interaction: discord.Interaction) -> bool:
        return await _is_operator(
            bot, interaction.user.id, user_role_ids(interaction.user)
        )

    @tree.command(name="devin", description="Start a Devin Cloud session")
    @app_commands.describe(
        prompt="What Devin should do",
        model="Model picker (e.g. swe2-max, opus, fusion) — overrides mode",
        repo="org/repo (comma-separate for several)",
        issue="GitHub issue to attach as context (URL, owner/repo#n, or #n)",
        branch="Base branch to work from (added to the prompt)",
        mode="Agent mode (default from env)",
        title="Thread/session title",
        budget="Per-task ACU cap — session parks when burn reaches it",
        attachment="Photo/file to attach (screenshot, log, spec)",
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
        budget: int | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        if not await _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        if budget is not None and budget <= 0:
            await interaction.response.send_message(
                "`budget:` must be a positive number of ACUs.", ephemeral=True
            )
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
                bot, prompt=prompt, repos=repos, model=model, mode=mode,
                title=title, budget=budget,
                attachment_urls=[attachment.url] if attachment else None,
                spawned_by=str(interaction.user.id),
            )
        except SpawnError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        await interaction.followup.send(f"Session started → {thread.mention}")

    @tree.command(
        name="chain",
        description="Run a playbook — chained Devin phases, one session each",
    )
    @app_commands.describe(
        playbook="janitor: audit→fix→review→merge · iterate: implement→review→apply→merge",
        prompt="The task/instructions for the first phase",
        repo="org/repo (comma-separate for several)",
        budget="Chain-wide ACU cap (default: MAX_ACU_LIMIT × phases)",
        auto="Run every phase without tapping Continue (default: ask at mutating phases)",
        title="Thread title prefix",
        attachment="Photo/file for the first phase only",
    )
    @app_commands.choices(
        playbook=[app_commands.Choice(name=k, value=k) for k in PLAYBOOKS]
    )
    @app_commands.autocomplete(repo=repo_autocomplete)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def chain_cmd(
        interaction: discord.Interaction,
        playbook: str,
        prompt: str,
        repo: str | None = None,
        budget: float | None = None,
        auto: bool = False,
        title: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        if not await _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        if budget is not None and budget <= 0:
            await interaction.response.send_message(
                "`budget:` must be a positive number of ACUs.", ephemeral=True
            )
            return
        phases = PLAYBOOKS.get(playbook)
        if not phases:
            await interaction.response.send_message(
                f"Unknown playbook {playbook!r} — choices: "
                + ", ".join(PLAYBOOKS),
                ephemeral=True,
            )
            return
        if not bot.settings.github_enabled and any(
            p.gate == "single_pr" or p.action == "arm_automerge"
            for p in phases
        ):
            # without the App the prs table never fills, so a PR gate
            # can't see what Devin opened and auto-merge can't arm —
            # the chain would halt at review with "no single PR produced"
            await interaction.response.send_message(
                f"`{playbook}` needs the GitHub App (GITHUB_APP_*) — its "
                "PR gate and auto-merge phases can't work without it.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        repos = _split_repos(repo)
        cap = (
            float(budget)
            if budget
            else (
                float(bot.settings.max_acu_limit) * len(phases)
                if bot.settings.max_acu_limit
                else None
            )
        )
        base_title = title or f"{playbook}: {prompt[:60]}"
        chain = {
            "playbook": playbook,
            "step": 0,
            "pending": None,
            "cap": cap,
            "spent": 0.0,
            "pr_key": "",
            "orig": prompt,
            "auto": bool(auto),
            # stable base for phase thread titles ("<base> · <phase>")
            "title": base_title,
        }
        prompt0 = render_prompt(
            phases[0], {"orig": prompt, "repo": ",".join(repos or [])}
        )
        try:
            session, thread = await spawn_session(
                bot,
                prompt=prompt0,
                repos=repos,
                title=base_title,
                budget=cap,
                chain=chain,
                attachment_urls=[attachment.url] if attachment else None,
                spawned_by=str(interaction.user.id),
            )
        except SpawnError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        await interaction.followup.send(
            f"Chain `{playbook}` started → {thread.mention}"
        )
        await thread.send(
            f"⛓️ chain **{playbook}** — "
            + " → ".join(f"`{p.name}`" for p in phases)
            + (
                ""
                if auto
                else " — mutating phases wait for a **Continue →** tap"
            )
        )

    @tree.command(name="chains", description="List playbook chains")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def chains_cmd(interaction: discord.Interaction) -> None:
        if not await _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        rows = await bot.db.chained_bindings()
        if not rows:
            await interaction.response.send_message(
                "No chains yet.", ephemeral=True
            )
            return
        embed = discord.Embed(title="Playbook chains")
        for b in rows:
            c = b.chain or {}
            phases = PLAYBOOKS.get(str(c.get("playbook") or ""), [])
            step = int(c.get("step") or 0)
            pend = c.get("pending")
            shown = int(pend) if pend is not None else step
            phase_name = phases[shown].name if shown < len(phases) else "done"
            bits = [
                f"`{c.get('playbook')}` step "
                f"{min(step + 1, len(phases))}/{len(phases)} `{phase_name}`"
            ]
            if pend is not None:
                # pending holds the NEXT phase's index — name it, not the
                # completed one
                bits.append("⏸ awaiting Continue tap")
            if c.get("halted"):
                bits.append(f"🏁 {c['halted']}")
            if c.get("spent"):
                bits.append(f"{c['spent']:g} ACU")
            bits.append(f"<#{b.thread_id}>")
            embed.add_field(
                name=(b.title or b.session_id)[:100],
                value=" · ".join(bits),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(
        name="devin-all", description="Start one session per repo (fan-out)"
    )
    @app_commands.describe(
        prompt="What Devin should do in each repo",
        repos="Comma-separated owner/repo list (2+)",
        model="Model picker — overrides mode",
        mode="Agent mode (default from env)",
        title="Thread title prefix",
        attachment="Photo/file attached to every spawned session",
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
        attachment: discord.Attachment | None = None,
    ) -> None:
        if not await _allowed(interaction):
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
                    attachment_urls=[attachment.url] if attachment else None,
                    spawned_by=str(interaction.user.id),
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
        every="Interval: 30m, 6h, 1d, …",
        prompt="What Devin should do each run (or pick a recipe)",
        recipe="Canned task — dep-audit, test-coverage, security-scan",
        repo="org/repo (comma-separate for several)",
        model="Model picker (optional)",
        kind="spawn = run the prompt; digest = post the window rollup",
        watch="monitor kind: https://… URL or ci:owner/repo[@branch]",
        expect="monitor kind: substring the URL body must contain",
        cooldown="monitor kind: still-red re-fire floor (default 4h)",
    )
    @app_commands.choices(
        recipe=[app_commands.Choice(name=k, value=k) for k in RECIPES],
        kind=[
            app_commands.Choice(name="spawn", value="spawn"),
            app_commands.Choice(name="digest", value="digest"),
            app_commands.Choice(name="monitor", value="monitor"),
            app_commands.Choice(name="inbox", value="inbox"),
        ],
    )
    @app_commands.autocomplete(model=model_autocomplete, repo=repo_autocomplete)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def schedule_cmd(
        interaction: discord.Interaction,
        every: str,
        prompt: str | None = None,
        recipe: str | None = None,
        repo: str | None = None,
        model: str | None = None,
        kind: str | None = None,
        watch: str | None = None,
        expect: str | None = None,
        cooldown: str | None = None,
    ) -> None:
        if not await _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        kind = kind or "spawn"
        if kind in {"digest", "inbox"}:
            # prompt is just a label — the rollup covers ALL activity in
            # the window, so spawn params would be silently ignored
            prompt = prompt or kind
            repo = model = recipe = None
            watch = expect = cooldown = None
        elif recipe:
            prompt = f"{RECIPES[recipe]}\n\n{prompt}" if prompt else RECIPES[recipe]
        if kind == "monitor":
            parsed = parse_watch(watch or "")
            if parsed is None:
                await interaction.response.send_message(
                    "`watch:` must be `https://…` or `ci:owner/repo[@branch]`.",
                    ephemeral=True,
                )
                return
            if expect and parsed[0] != "url":
                await interaction.response.send_message(
                    "`expect:` only applies to URL watches.", ephemeral=True
                )
                return
            if parsed[0] == "ci" and not bot.settings.github_enabled:
                await interaction.response.send_message(
                    "`ci:` watches need the GitHub App (GITHUB_APP_*).",
                    ephemeral=True,
                )
                return
            if not prompt:
                await interaction.response.send_message(
                    "A monitor needs a `prompt:` — the fix instructions "
                    "when the check goes red.", ephemeral=True,
                )
                return
        if kind != "monitor" and (watch or expect or cooldown):
            await interaction.response.send_message(
                "`watch:`/`expect:`/`cooldown:` only apply to `kind:monitor`.",
                ephemeral=True,
            )
            return
        if not prompt:
            await interaction.response.send_message(
                "Give a `prompt:` or pick a `recipe:`.", ephemeral=True
            )
            return
        interval = parse_every(every)
        if interval is None or interval < 300:
            await interaction.response.send_message(
                "Interval must be like `30m`, `6h`, `1d` (min 5m).", ephemeral=True
            )
            return
        cooldown_seconds = 14400
        if cooldown:
            cd = parse_every(cooldown)
            if cd is None or cd < 300:
                await interaction.response.send_message(
                    "`cooldown:` must be like `30m`, `4h`, `1d` (min 5m).",
                    ephemeral=True,
                )
                return
            cooldown_seconds = cd
        row = ScheduleRow(
            id=0,
            prompt=prompt,
            repos=_split_repos(repo) or [],
            model=model,
            interval_seconds=interval,
            next_run_at=int(time.time()) + interval,
            kind=kind,
            watch=watch or "",
            expect=expect or "",
            cooldown_seconds=cooldown_seconds,
            spawned_by=str(interaction.user.id),
        )
        row.id = await bot.db.add_schedule(row)
        detail = (
            "posts the activity rollup to the hub channel"
            if kind == "digest"
            else "posts the triage card to the hub channel"
            if kind == "inbox"
            else f"watches `{watch}` — spawns only on a red edge "
                 f"(cooldown {cooldown_seconds // 60}m)"
            if kind == "monitor"
            else f"`{prompt[:80]}` on {', '.join(row.repos) or 'default repos'}"
        )
        await interaction.response.send_message(
            f"Scheduled #{row.id} — every {every}, first run in {every}.\n"
            f"{detail}",
            ephemeral=True,
        )

    @tree.command(
        name="continue",
        description="Continue this session's work in a fresh session",
    )
    @app_commands.describe(
        notes="Extra instructions for the continuation",
        attachment="Fresh screenshot/file for the continuation",
    )
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def continue_cmd(
        interaction: discord.Interaction,
        notes: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        if not await _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Run /continue inside a session thread.", ephemeral=True
            )
            return
        binding = await bot.db.get_binding_by_thread(interaction.channel.id)
        if binding is None:
            await interaction.response.send_message(
                "This thread isn't bound to a session.", ephemeral=True
            )
            return
        if binding.status not in {"exit", "error", "suspended"}:
            await interaction.response.send_message(
                "Session is still active — reply here to steer it "
                "(or /kill first).", ephemeral=True,
            )
            return
        await interaction.response.defer()
        sess = None
        try:
            sess = await bot.devin.get_session(binding.session_id)
        except Exception:  # noqa: BLE001 — last_msg is the fallback
            log.info("continue: get_session failed for %s", binding.session_id)
        so = (sess.structured_output if sess else None) or {}
        prompt = (
            "Continue the work from a previous Devin session "
            f"({binding.url or binding.session_id}).\n"
            f"Summary so far: "
            f"{so.get('summary') or binding.last_msg or '(none captured)'}\n"
        )
        if so.get("files_changed"):
            prompt += f"Files touched: {', '.join(so['files_changed'][:20])}\n"
        if notes:
            prompt += f"\nAdditional instructions: {notes}\n"
        prompt += "\nInspect the repo state first, then carry the task forward."
        try:
            _, thread = await spawn_session(
                bot,
                prompt=prompt,
                repos=binding.repos.split(",") if binding.repos else None,
                model=binding.model or None,
                title=f"{binding.title or 'session'} (cont.)",
                continued_from=binding.session_id,
                budget=binding.max_acu,
                # a chain phase's /continue child IS the phase retry —
                # carried chain state keeps the playbook advancing
                chain=continued_chain(binding.chain, sess),
                attachment_urls=[attachment.url] if attachment else None,
                # ownership transfers — the tapper gets the pings now
                spawned_by=str(interaction.user.id),
            )
        except SpawnError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        await interaction.followup.send(
            f"Continuation spawned → {thread.mention}", ephemeral=True
        )
        await interaction.channel.send(f"↪️ Continued in {thread.mention}")

    @tree.command(name="schedules", description="List recurring Devin tasks")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def schedules_cmd(interaction: discord.Interaction) -> None:
        if not await _allowed(interaction):
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
            tag = f" · {s.kind}" if s.kind != "spawn" else ""
            watch_state = (
                f" ({s.watch_state})" if s.kind == "monitor" and s.watch_state
                else ""
            )
            embed.add_field(
                name=f"#{s.id} · every {s.interval_seconds // 60}m · {state}{tag}",
                value=(
                    f"`{s.watch}{watch_state}` watches → `{s.prompt[:60]}`"
                    f"\n{due}"
                    if s.kind == "monitor"
                    else f"`{s.prompt[:80]}`\n"
                    f"{', '.join(s.repos) or 'default repos'} · {due}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="unschedule", description="Delete a recurring task")
    @app_commands.describe(schedule_id="Schedule id from /schedules")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def unschedule_cmd(interaction: discord.Interaction, schedule_id: int) -> None:
        if not await _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        row = await bot.db.get_schedule(schedule_id)
        if row is None:
            await interaction.response.send_message(
                f"No schedule #{schedule_id}.", ephemeral=True
            )
            return
        if not _may_destroy(
            bot.settings, interaction.user.id, row.spawned_by
        ):
            await interaction.response.send_message(
                _OWNER_OR_ADMIN, ephemeral=True
            )
            return
        await bot.db.delete_schedule(schedule_id)
        await interaction.response.send_message(
            f"Deleted schedule #{schedule_id}.", ephemeral=True
        )

    @tree.command(
        name="digest",
        description="Rollup of what Devin did — sessions grouped by outcome",
    )
    @app_commands.describe(hours="Lookback window (default 24h, max 168h)")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def digest_cmd(
        interaction: discord.Interaction, hours: int = 24
    ) -> None:
        if not await _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        hours = max(1, min(hours, 168))
        since = int(time.time()) - hours * 3600
        embed = build_digest(await bot.db.bindings_since(since), since)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(
        name="inbox",
        description="Triage card — everything waiting on a human tap",
    )
    @app_commands.describe(
        mine="Only items you spawned — monitors + open PRs stay shared"
    )
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def inbox_cmd(
        interaction: discord.Interaction, mine: bool = False
    ) -> None:
        if not await _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        await interaction.response.send_message(
            embed=await build_inbox_embed(
                bot.db,
                owner=str(interaction.user.id) if mine else "",
            ),
            ephemeral=True,
        )

    @tree.command(
        name="note",
        description="Save a standing note — injected into every future "
        "session's prompt for that repo",
    )
    @app_commands.describe(
        text="The guidance (e.g. 'tests are flaky — run pytest -x')",
        repo="owner/repo (default: this thread's session repo)",
    )
    @app_commands.autocomplete(repo=repo_autocomplete)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def note_cmd(
        interaction: discord.Interaction, text: str, repo: str | None = None
    ) -> None:
        if not await _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        target = (repo or "").strip()
        if not target and isinstance(interaction.channel, discord.Thread):
            # phone flow: /note inside a session thread keys on its repo
            binding = await bot.db.get_binding_by_thread(
                interaction.channel.id
            )
            if binding is not None and binding.repos:
                target = binding.repos.split(",")[0]
        if not target:
            await interaction.response.send_message(
                "Pass `repo:` (or run inside a session thread).",
                ephemeral=True,
            )
            return
        if await bot.db.add_note(
            target, text.strip(), str(interaction.user.id)
        ):
            await interaction.response.send_message(
                f"Noted for `{target}` — future sessions will see it.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "That note already exists for this repo.", ephemeral=True
            )

    @tree.command(name="notes", description="List standing repo notes")
    @app_commands.describe(repo="Filter to one repo (default: all repos)")
    @app_commands.autocomplete(repo=repo_autocomplete)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def notes_cmd(
        interaction: discord.Interaction, repo: str | None = None
    ) -> None:
        if not await _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        rows = await bot.db.list_notes(repo)
        if not rows:
            await interaction.response.send_message(
                "No notes yet.", ephemeral=True
            )
            return
        embed = discord.Embed(title="Repo notes")
        for nid, r, note in rows:
            embed.add_field(name=f"#{nid} · {r}", value=note[:200],
                            inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="unnote", description="Delete a repo note")
    @app_commands.describe(note_id="Note id from /notes")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def unnote_cmd(
        interaction: discord.Interaction, note_id: int
    ) -> None:
        if not await _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        owner = await bot.db.note_created_by(note_id)
        if owner is None:
            await interaction.response.send_message(
                f"No note #{note_id}.", ephemeral=True
            )
            return
        if not _may_destroy(bot.settings, interaction.user.id, owner):
            await interaction.response.send_message(
                _OWNER_OR_ADMIN, ephemeral=True
            )
            return
        await bot.db.delete_note(note_id)
        await interaction.response.send_message(
            f"Deleted note #{note_id}.", ephemeral=True
        )

    @tree.command(name="sessions", description="List sessions started through this bot")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def sessions_cmd(interaction: discord.Interaction) -> None:
        if not await _allowed(interaction):
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
        if not await _allowed(interaction):
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
        if not _may_destroy(
            bot.settings, interaction.user.id, binding.spawned_by
        ):
            await interaction.response.send_message(
                _OWNER_OR_ADMIN, ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        # DELETE /v3/.../sessions/{id} is verified live (probe_admin.py) —
        # it terminates the run; the record stays listable. Report the
        # outcome instead of swallowing it.
        try:
            await bot.devin.terminate_session(binding.session_id)
            verdict = "terminated"
        except Exception as e:  # noqa: BLE001 — surface the refusal
            verdict = (
                f"the API refused delete ({e}) — "
                "it'll idle-suspend on its own"
            )
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
            f"Parked `{binding.session_id}` ({verdict}), archived its thread.",
            ephemeral=True,
        )

    async def _resolve_binding(
        interaction: discord.Interaction, session: str | None
    ) -> Binding | None:
        """The kill/delete/rename lookup: explicit `session:` id, else
        the current thread's binding."""
        if session:
            return await bot.db.get_binding(session)
        if isinstance(interaction.channel, discord.Thread):
            return await bot.db.get_binding_by_thread(interaction.channel.id)
        return None

    @tree.command(
        name="delete",
        description="Delete a session — terminates it and hides it from "
        "lists (the ACU still counts; `wipe:` also deletes the thread)",
    )
    @app_commands.describe(
        session="Session id (default: this thread's session)",
        wipe="Also delete the Discord thread (default: just archive it)",
    )
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def delete_cmd(
        interaction: discord.Interaction,
        session: str | None = None,
        wipe: bool = False,
    ) -> None:
        if not await _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        binding = await _resolve_binding(interaction, session)
        if binding is None:
            await interaction.response.send_message(
                "No such session — run inside its thread or pass `session:`.",
                ephemeral=True,
            )
            return
        if not _may_destroy(
            bot.settings, interaction.user.id, binding.spawned_by
        ):
            await interaction.response.send_message(
                _OWNER_OR_ADMIN, ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        # Same verified DELETE as /kill — it terminates; the record stays
        # listable server-side either way
        try:
            await bot.devin.terminate_session(binding.session_id)
            verdict = "terminated"
        except Exception as e:  # noqa: BLE001 — surface the refusal
            verdict = (
                f"the API refused delete ({e}) — "
                "it'll idle-suspend on its own"
            )
        binding.deleted = True
        binding.active = False
        await bot.db.upsert_binding(binding)
        thread = await interaction.client.fetch_channel(binding.thread_id)
        wiped = False
        if isinstance(thread, discord.Thread):
            if wipe:
                try:
                    await thread.delete()
                    wiped = True
                except discord.HTTPException:
                    pass
            if not wiped:
                await thread.send("Session deleted — no longer listed.")
                try:
                    await thread.edit(archived=True)
                except discord.HTTPException:
                    pass
        await interaction.followup.send(
            f"Deleted `{binding.session_id}` ({verdict})"
            + (" — thread removed." if wiped else " — thread archived.")
            + " It stays in /usage — the ACU burned still counts.",
            ephemeral=True,
        )

    @tree.command(
        name="rename",
        description="Rename a session — Discord-side (Devin has no rename "
        "API): thread title + binding title",
    )
    @app_commands.describe(
        title="New title",
        session="Session id (default: this thread's session)",
    )
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def rename_cmd(
        interaction: discord.Interaction,
        title: str,
        session: str | None = None,
    ) -> None:
        if not await _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        title = title.strip()
        if not title:
            await interaction.response.send_message(
                "Give a non-empty `title:`.", ephemeral=True
            )
            return
        binding = await _resolve_binding(interaction, session)
        if binding is None:
            await interaction.response.send_message(
                "No such session — run inside its thread or pass `session:`.",
                ephemeral=True,
            )
            return
        binding.title = title
        await bot.db.upsert_binding(binding)
        renamed = False
        try:
            thread = await interaction.client.fetch_channel(binding.thread_id)
            if isinstance(thread, discord.Thread):
                await thread.edit(name=title[:90])
                renamed = True
        except discord.HTTPException:
            pass
        await interaction.response.send_message(
            f"Renamed to **{title[:100]}**"
            + ("" if renamed else " (binding only — thread rename failed)"),
            ephemeral=True,
        )

    @tree.command(name="usage", description="ACU burn rollup across bot sessions")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def usage_cmd(interaction: discord.Interaction) -> None:
        if not await _allowed(interaction):
            await interaction.response.send_message(NOT_ALLOWED, ephemeral=True)
            return
        # include_deleted — deleted sessions still burned their ACU; the
        # accounting view counts them even though lists hide them
        bindings = await bot.db.all_bindings(limit=1000, include_deleted=True)
        if not bindings:
            await interaction.response.send_message(
                "No sessions yet.", ephemeral=True
            )
            return
        s = usage_summary(bindings, time.time())
        embed = discord.Embed(title="Devin usage")
        embed.add_field(
            name="ACU burn",
            value=(
                f"today **{s['today']:g}** · last 7d **{s['week']:g}** · "
                f"all-time **{s['total']:g}**\n"
                f"({s['count']} sessions — buckets by session start)"
            ),
            inline=False,
        )
        if s["top"]:
            embed.add_field(
                name="Top sessions",
                value="\n".join(
                    f"**{b.acus:g}** — {(b.title or b.session_id)[:60]} <#{b.thread_id}>"
                    for b in s["top"]
                ),
                inline=False,
            )
        if s["by_repo"]:
            embed.add_field(
                name="By repo",
                value="\n".join(
                    f"`{repo}` — {acus:g} ACU ({n} session{'s' if n > 1 else ''})"
                    for repo, (acus, n) in s["by_repo"].items()
                ),
                inline=False,
            )
        if s["by_user"]:
            embed.add_field(
                name="By user",
                value="\n".join(
                    f"{spawned_by_label(who)} — "
                    f"{acus:g} ACU ({n} session{'s' if n > 1 else ''})"
                    for who, (acus, n) in s["by_user"].items()
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="devin-status", description="Refresh a session's status")
    @app_commands.describe(session="Session id (default: most recent)")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def devin_status_cmd(
        interaction: discord.Interaction, session: str | None = None
    ) -> None:
        if not await _allowed(interaction):
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
            embed=status_embed(
                sess, fallback_title=binding.title,
                spawned_by=binding.spawned_by,
            ),
            ephemeral=True,
        )

    # ---- Runtime allowlist — hard-gated on TEAM_ADMIN_IDS. Without admins
    # configured the commands refuse entirely: operators must never be able
    # to grant themselves (or anyone) access by bot command. -------------

    @tree.command(
        name="allow", description="Grant a user bot access (admin only)"
    )
    @app_commands.describe(user="The Discord user to allow")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def allow_cmd(
        interaction: discord.Interaction, user: discord.User
    ) -> None:
        if not bot.settings.admin_user_id_set:
            await interaction.response.send_message(
                "No admins configured — set `TEAM_ADMIN_IDS` first "
                "(or use `ALLOWED_USER_IDS`/`REQUIRED_ROLE_ID`).",
                ephemeral=True,
            )
            return
        if not bot.settings.is_admin(interaction.user.id):
            await interaction.response.send_message(
                "Admins only.", ephemeral=True
            )
            return
        await bot.db.add_allowed_user(user.id, interaction.user.id)
        await interaction.response.send_message(
            f"Allowed {user.mention}.", ephemeral=True
        )

    @tree.command(
        name="deny", description="Revoke runtime-granted access (admin only)"
    )
    @app_commands.describe(user="The Discord user to remove")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def deny_cmd(
        interaction: discord.Interaction, user: discord.User
    ) -> None:
        if not bot.settings.admin_user_id_set:
            await interaction.response.send_message(
                "No admins configured — set `TEAM_ADMIN_IDS` first.",
                ephemeral=True,
            )
            return
        if not bot.settings.is_admin(interaction.user.id):
            await interaction.response.send_message(
                "Admins only.", ephemeral=True
            )
            return
        removed = await bot.db.remove_allowed_user(user.id)
        msg = (
            f"Removed {user.mention}."
            if removed
            else f"{user.mention} isn't on the runtime list."
        )
        if user.id in bot.settings.allowed_user_id_set:
            msg += " (Still allowlisted via `ALLOWED_USER_IDS`.)"
        elif (
            bot.settings.required_role_id
            and interaction.guild is not None
            and (m := interaction.guild.get_member(user.id)) is not None
            and bot.settings.required_role_id
            in (r.id for r in m.roles)
        ):
            msg += " (Still allowed via the guild role.)"
        await interaction.response.send_message(msg, ephemeral=True)

import asyncio
import io
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import discord

from .acp_bridge import SessionStream
from .chains import (
    PLAYBOOKS,
    Ask,
    Halt,
    Phase,
    advance,
    continued_chain,
    render_prompt,
)
from .choices import parse_choices
from .config import Settings
from .db import Binding, Database, PrRow
from .devin_client import DevinClient
from .embeds import (
    completion_embed,
    mention_for,
    pr_embed,
    status_embed,
)
from .github_client import (
    GithubClient,
    PullRef,
    parse_issue_ref,
    parse_pr_url,
)
from .models import Session, SessionMessage
from .progress import ProgressTracker, chunk_text_from, summarize_update
from .spawn import SpawnError, spawn_session
from .views import (
    ChoiceView,
    CompletionView,
    ComponentHandler,
    FixCIView,
    PRView,
)

if TYPE_CHECKING:
    from .bot.main import DevinMobileBot

log = logging.getLogger(__name__)

DISCORD_MSG_LIMIT = 1900  # headroom under the 2000 cap
QUIET_STATUSES = {"exit", "error", "suspended"}


def chunk_text(text: str, limit: int = DISCORD_MSG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


ATTACHMENT_RE = re.compile(r"ATTACHMENT:(\{[^\n]*\})")


def extract_attachments(text: str) -> tuple[str, list[str]]:
    """Split Devin's inline `ATTACHMENT:{json}` markers off the message text."""
    urls: list[str] = []

    def repl(m: re.Match) -> str:
        try:
            url = json.loads(m.group(1)).get("url")
            if url:
                urls.append(url)
        except ValueError:
            pass
        return ""

    return ATTACHMENT_RE.sub(repl, text).strip(), urls


def relayable(items: list[SessionMessage], seen: list[str]) -> list[SessionMessage]:
    """Filter to new Devin messages; `seen` is an insertion-ordered id list
    (membership via a scratch set, order preserved for FIFO eviction)."""
    have = set(seen)
    out = []
    for m in items:
        if m.event_id in have:
            continue
        have.add(m.event_id)
        seen.append(m.event_id)
        if m.source == "devin":
            out.append(m)
    return out


@dataclass
class Notification:
    kind: str  # input | turn_end | approval | complete | suspended | error
    text: str
    mention: bool = True


# status_details on suspend that are routine — the tail of a finished turn,
# not something worth a phone push.
QUIET_SUSPEND_REASONS = {"inactivity", "user_request"}


def classify_transition(
    old_status: str | None,
    old_detail: str | None,
    session: Session,
    last_devin_msg: str | None = None,
    msg_fresh: bool = False,
) -> Notification | None:
    s, d = session.status, session.status_detail
    if s != old_status:
        if s == "exit":
            return Notification("complete", "Session finished.")
        if s == "error":
            return Notification("error", "Session errored.")
        if s == "suspended":
            if d in QUIET_SUSPEND_REASONS or d is None:
                return Notification(
                    "suspended",
                    f"Session suspended ({d or 'no reason'}) — reply here to resume it.",
                    mention=False,
                )
            return Notification(
                "suspended", f"Session suspended ({d}) — needs attention."
            )
    if d != old_detail:
        if d == "waiting_for_user":
            # fires on every turn end — only call it "asking" when the last
            # message really is a question
            if last_devin_msg and last_devin_msg.rstrip().endswith("?"):
                if msg_fresh:
                    # the question was just relayed into the thread — the
                    # ping still fires (it's the push) but quoting it again
                    # would post the same text twice back to back
                    return Notification(
                        "input", "Devin is asking a question ⤴"
                    )
                excerpt = last_devin_msg.strip()[-240:]
                return Notification("input", f"Devin is asking: “{excerpt}”")
            return Notification(
                "turn_end", "Devin finished its turn — reply here to continue."
            )
        if d == "waiting_for_approval":
            return Notification(
                "approval",
                "Devin needs an approval — tap Approve or open the session.",
            )
        if d == "finished":
            return Notification("complete", "Session finished.")
    return None


class Relay:
    def __init__(
        self,
        bot: discord.Client,
        devin: DevinClient,
        db: Database,
        settings: Settings,
        github: "GithubClient | None" = None,
        component_handler: "ComponentHandler | None" = None,
        streamer: "SessionStream | None" = None,
    ) -> None:
        self.bot = bot
        self.devin = devin
        self.db = db
        self.settings = settings
        self.github = github
        self._component_handler = component_handler
        self.streamer = streamer
        self.progress = ProgressTracker(self._thread)
        self._locks: dict[str, asyncio.Lock] = {}
        self._repoll: set[str] = set()  # sessions needing one more pass
        self._tasks: set[asyncio.Task] = set()
        self._last_presence = -1  # forces one presence write on first tick

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def request_poll(self, binding: Binding) -> None:
        """Schedule an out-of-band poll for one binding — used right after
        we send Devin a message so the reply doesn't wait for the tick.
        Requests arriving while a poll is in flight coalesce into a single
        re-poll instead of queueing unbounded tasks."""
        if self._lock(binding.session_id).locked():
            self._repoll.add(binding.session_id)
            return
        task = asyncio.create_task(self._safe_poll(binding))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _safe_poll(self, binding: Binding) -> None:
        try:
            await self.poll_binding(binding)
        except Exception:
            log.exception("on-demand poll failed for %s", binding.session_id)

    async def aclose(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.streamer is not None:
            await self.streamer.aclose()

    async def run_forever(self) -> None:
        try:
            await self._resume_chains()
        except Exception:
            log.exception("chain resume sweep failed")
        while True:
            try:
                bindings = await self.db.active_bindings()
                await self._presence(len(bindings))
                for binding in bindings:
                    try:
                        await self.poll_binding(binding)
                    except Exception:
                        log.exception("poll failed for %s", binding.session_id)
            except Exception:
                log.exception("poll loop error")
            await asyncio.sleep(self.settings.poll_interval_seconds + random.uniform(0, 3))

    async def poll_binding(self, binding: Binding) -> None:
        async with self._lock(binding.session_id):
            await self._poll(binding)
            # requests that landed while we polled get exactly one extra pass
            while binding.session_id in self._repoll:
                self._repoll.discard(binding.session_id)
                await self._poll(binding)

    async def _poll(self, binding: Binding) -> None:
        cursor = binding.msg_cursor
        had_activity = False
        msg_fresh = False
        while True:
            page = await self.devin.list_messages(binding.session_id, after=cursor)
            for m in relayable(page.items, binding.seen_event_ids):
                had_activity = True
                clean, _ = extract_attachments(m.message or "")
                if clean:
                    # persisted on the binding so a waiting_for_user flip in a
                    # LATER tick still sees the question text — the message and
                    # the status change routinely land in different polls
                    binding.last_msg = clean
                    msg_fresh = True
                await self._relay_message(binding, m)
            if page.end_cursor:
                cursor = page.end_cursor
            if not page.has_next_page or not page.items:
                break
        binding.msg_cursor = cursor

        session = await self.devin.get_session(binding.session_id)
        await self._sync_prs(binding, session)
        await self._typing(binding, session)
        notif = classify_transition(
            binding.status, binding.status_detail, session, binding.last_msg,
            msg_fresh=msg_fresh,
        )
        had_activity = had_activity or notif is not None
        if had_activity:
            binding.last_activity_at = int(time.time())
            binding.quiet_alerted = False
        # Derived-active BEFORE _check_acu — a budget-hit park must not be
        # overwritten by the status-derived value on the same tick.
        binding.active = session.status not in QUIET_STATUSES
        binding.acus = session.acus_consumed
        await self._check_acu(binding, session)
        if (
            not binding.active
            and session.status in QUIET_STATUSES
            and await self.db.has_armed_open_pr(binding.session_id)
        ):
            # an armed auto-merge PR on a dead session still needs polling
            # — that's the mechanism that fires the merge once CI greens
            binding.active = True
        if session.title:
            binding.title = session.title
        binding.status = session.status
        binding.status_detail = session.status_detail
        binding.url = session.url
        is_complete = notif is not None and notif.kind == "complete"
        await self._update_anchor(binding, session, complete=is_complete)
        if notif:
            await self._notify(binding, session, notif)
        if notif is not None and notif.kind == "complete" and binding.chain:
            await self._advance_chain(binding, session)
        if notif is not None and notif.kind == "error":
            respawned = await self._maybe_respawn(binding, session)
            if not respawned and binding.chain:
                await self._note_chain_halt(binding)
        await self._watchdog(binding, session)
        await self.db.upsert_binding(binding)
        # Attach the session to the live-progress stream — idempotent, and
        # self-heals after a stream reconnect (attached set is cleared then).
        if self.streamer is not None and binding.active:
            try:
                await self.streamer.attach(binding.session_id)
            except Exception:  # noqa: BLE001 — progress is best-effort
                log.debug("stream attach failed for %s", binding.session_id)

    async def on_progress(self, session_id: str, update: dict) -> None:
        """SessionStream callback — a session/update notification arrived on
        the bridge socket for an attached session."""
        if update.get("sessionUpdate") == "agent_message_chunk":
            text = chunk_text_from(update)
            if text:
                binding = await self.db.get_binding(session_id)
                if binding is not None:
                    await self.progress.stream_chunk(binding, text)
            return
        lines = summarize_update(update)
        if not lines:
            return
        binding = await self.db.get_binding(session_id)
        if binding is None:
            return
        for line in lines:
            await self.progress.push(binding, line)

    # ---- budget + liveness ------------------------------------------------

    async def _check_acu(self, binding: Binding, session: Session) -> None:
        """Ping once when ACU burn crosses 80%/100% of the configured cap —
        the cap being hit mid-task is exactly when a phone ping matters."""
        # Per-task cap (budget: on /devin) wins over the global default.
        cap = binding.max_acu or self.settings.max_acu_limit
        if not cap or cap <= 0:
            return
        frac = session.acus_consumed / cap
        for bit, threshold in ((1, 0.8), (2, 1.0)):
            if frac >= threshold and not binding.acu_warned & bit:
                binding.acu_warned |= bit
                label = (
                    "hit the cap" if threshold >= 1.0 else "is nearing the cap"
                )
                await self._notify(
                    binding,
                    session,
                    Notification(
                        "acu",
                        f"ACU usage {label}: {session.acus_consumed:g} of "
                        f"{cap} ACUs consumed."
                        + (" — parked." if threshold >= 1.0 else ""),
                    ),
                )
                if threshold >= 1.0:
                    # v3's own max_acu_limit enforces server-side; this parks
                    # the binding so our side stops polling/pinging too.
                    binding.active = False

    async def _watchdog(self, binding: Binding, session: Session) -> None:
        """A 'running' session that hasn't said anything in a while posts one
        quiet note — never a mention, it's almost always still working."""
        minutes = self.settings.silence_alert_minutes
        if minutes <= 0 or session.status != "running" or binding.quiet_alerted:
            return
        anchor = binding.last_activity_at or binding.created_at
        elapsed = time.time() - anchor
        if elapsed < minutes * 60:
            return
        binding.quiet_alerted = True
        thread = await self._thread(binding)
        if thread is None:
            return
        await thread.send(
            f"Devin has been quiet for ~{int(elapsed // 60)}min — "
            "likely still working. Reply here to nudge it."
        )

    # Statuses that mean "Devin is actively working right now". waiting_for_*
    # details mean the turn ended — no typing dots for a parked turn.
    _WORKING_DETAILS = {"working", "running", None}

    async def _typing(self, binding: Binding, session: Session) -> None:
        """Show the bot as 'typing…' in the thread while Devin is mid-turn —
        v3 exposes no tool-call progress, so this is the liveness signal."""
        if session.status not in {"running", "claimed", "resuming", "new"}:
            return
        if session.status_detail in {"waiting_for_user", "waiting_for_approval"}:
            return
        thread = await self._thread(binding)
        if thread is None:
            return
        try:
            await thread.typing()  # one-shot ~10s indicator per poll tick
        except discord.HTTPException:
            pass

    # ---- PR tracking -------------------------------------------------------

    async def _sync_prs(self, binding: Binding, session: Session) -> None:
        """Discover PRs from the v3 session payload; post a card once per PR,
        then poll state/CI each tick for notification transitions."""
        if self.github is None:
            return  # no app → no cards; PR links still show in the anchor embed
        for pr in session.pull_requests:
            row = await self.db.get_pr(binding.session_id, pr.pr_url)
            ref = parse_pr_url(pr.pr_url)
            if row is None:
                row = PrRow(
                    session_id=binding.session_id,
                    pr_url=pr.pr_url,
                    owner=ref.owner if ref else "",
                    repo=ref.repo if ref else "",
                    number=ref.number if ref else 0,
                    state=pr.pr_state or "open",
                )
                await self.db.upsert_pr(row)
                await self._post_pr_card(binding, row)
            if self.github is not None and ref and row.state not in (
                "merged", "closed"
            ):
                await self._poll_pr(binding, row, ref)

    async def _post_pr_card(self, binding: Binding, row: PrRow) -> None:
        thread = await self._thread(binding)
        if thread is None or not row.number:
            return
        if self.github is not None:
            try:
                data = await self.github.get_pr(
                    PullRef(owner=row.owner, repo=row.repo, number=row.number)
                )
                row.pr_title = data.get("title")
                row.checks_state = await self.github.get_checks(
                    PullRef(owner=row.owner, repo=row.repo, number=row.number)
                )
            except Exception:  # noqa: BLE001 — card still posts without details
                log.warning("PR detail fetch failed for %s", row.pr_url)
        card = await thread.send(
            embed=pr_embed(
                owner=row.owner, repo=row.repo, number=row.number,
                pr_title=row.pr_title, state=row.state,
                checks=row.checks_state, url=row.pr_url,
            ),
            view=(
                PRView(binding.session_id,
                       f"{row.owner}/{row.repo}#{row.number}", row.pr_url,
                       self._component_handler)
                if self._component_handler
                else discord.utils.MISSING
            ),
        )
        row.card_msg_id = card.id
        await self.db.upsert_pr(row)

    async def _poll_pr(self, binding: Binding, row: PrRow, ref: PullRef) -> None:
        try:
            data = await self.github.get_pr(ref)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 — transient GitHub errors are skippable
            log.warning("PR poll failed for %s", row.pr_url)
            return
        state = "merged" if data.get("merged") else data.get("state", row.state)
        checks = await self.github.get_checks(ref)  # type: ignore[union-attr]
        row.pr_title = data.get("title") or row.pr_title
        thread = await self._thread(binding)
        mention = mention_for(
            binding.spawned_by, self.settings.allowed_user_id_set
        )
        label = f"{row.repo}#{row.number}" if row.repo else f"PR #{row.number}"

        # Opt-in auto-merge runs every poll while conditions hold — it can't
        # piggyback on the transition dedupe (the flag may be toggled on
        # AFTER CI went green, when no further transition will ever arrive).
        if row.auto_merge and state == "open" and checks == "success":
            try:
                await self.github.merge_pr(  # type: ignore[union-attr]
                    ref, method=self.settings.github_merge_method
                )
                state = "merged"
                if thread is not None:
                    await thread.send(f"{mention} {label} auto-merged 🟣")
                # The auto-merge notice IS the merge notification — consume
                # the transition so the block below doesn't re-post it.
                row.state, row.checks_state = state, checks
                row.last_notified = f"{state}|{checks}"
                await self.db.upsert_pr(row)
                await self._refresh_pr_card(binding, row)
                return
            except Exception as e:  # noqa: BLE001
                log.warning("auto-merge failed for %s: %s", row.pr_url, e)
                if thread is not None:
                    await thread.send(f"{mention} {label} auto-merge failed: `{e}`")
                row.auto_merge = False  # don't retry the same failure forever

        # Composite transition key — notify once per distinct outcome.
        key = f"{state}|{checks}"
        if key == row.last_notified:
            row.state, row.checks_state = state, checks
            await self.db.upsert_pr(row)
            return
        if thread is not None:
            if state == "merged":
                await thread.send(f"{mention} {label} merged 🟣")
            elif state == "closed" and row.state != "closed":
                await thread.send(f"{label} closed.")
            elif checks == "success" and row.checks_state != "success":
                await thread.send(f"{mention} {label} — CI green ✅")
            elif checks == "failure" and row.checks_state != "failure":
                pr_key = ref.key
                view = (
                    FixCIView(binding.session_id, pr_key, self._component_handler)
                    if self._component_handler
                    else discord.utils.MISSING
                )
                await thread.send(
                    f"{mention} {label} — CI failing ❌", view=view
                )
        row.state, row.checks_state, row.last_notified = state, checks, key
        await self.db.upsert_pr(row)
        await self._refresh_pr_card(binding, row)

    async def _refresh_pr_card(self, binding: Binding, row: PrRow) -> None:
        if not row.card_msg_id:
            return
        thread = await self._thread(binding)
        if thread is None:
            return
        try:
            card = await thread.fetch_message(row.card_msg_id)
            await card.edit(embed=pr_embed(
                owner=row.owner, repo=row.repo, number=row.number,
                pr_title=row.pr_title, state=row.state,
                checks=row.checks_state, url=row.pr_url,
            ))
        except discord.HTTPException:
            pass

    async def _thread(self, binding: Binding) -> discord.Thread | None:
        chan = self.bot.get_channel(binding.thread_id) or await self.bot.fetch_channel(
            binding.thread_id
        )
        if not isinstance(chan, discord.Thread):
            return None
        if chan.archived:
            try:
                await chan.edit(archived=False)
            except discord.HTTPException:
                log.warning("could not unarchive thread %s", binding.thread_id)
        return chan

    async def _relay_message(self, binding: Binding, m: SessionMessage) -> None:
        thread = await self._thread(binding)
        if thread is None:
            log.warning("no thread %s for %s", binding.thread_id, binding.session_id)
            return
        # The real reply is arriving — a bare "thinking…" placeholder is
        # obsolete (a populated Working list survives mid-turn chat messages)
        await self.progress.clear_thinking(binding)
        text, attachments = extract_attachments(m.message or "")
        chunks = chunk_text(text) if text else []
        if text and await self.progress.reconcile(binding, text):
            # the reply already streamed live — the preview was dropped;
            # post the canonical text once and move on
            await self._post_text(thread, binding, chunks, attachments, text)
            return
        await self._post_text(thread, binding, chunks, attachments, text)

    async def _post_text(
        self,
        thread: discord.Thread,
        binding: Binding,
        chunks: list[str],
        attachments: list[str],
        text: str,
    ) -> None:
        """Post a relayed message's body chunks + attachment links. An
        enumerated question gets one button per option on the final send —
        choices sit at the tail of a message, so that's where they read."""
        lines = [
            chunk + (
                "\n… *(truncated — see session)*"
                if i == 7 and len(chunks) > 8
                else ""
            )
            for i, chunk in enumerate(chunks[:8])
        ]
        lines += [
            f"Attachment: [{url.rsplit('/', 1)[-1] or 'attachment'}]({url})"
            for url in attachments
        ]
        view: discord.ui.View | Any = discord.utils.MISSING
        if self._component_handler is not None:
            options = parse_choices(text)
            if options:
                view = ChoiceView(
                    binding.session_id, options, self._component_handler
                )
        for i, line in enumerate(lines):
            await thread.send(
                line, view=view if i == len(lines) - 1 else discord.utils.MISSING
            )

    async def _update_anchor(
        self, binding: Binding, session: Session, *, complete: bool = False
    ) -> None:
        if not binding.anchor_msg_id:
            return
        thread = await self._thread(binding)
        if thread is None:
            return
        embed_fn = completion_embed if complete else status_embed
        try:
            anchor = await thread.fetch_message(binding.anchor_msg_id)
            await anchor.edit(
                embed=embed_fn(
                    session, fallback_title=binding.title,
                    model=binding.model, spawned_by=binding.spawned_by,
                )
            )
        except discord.HTTPException:
            log.warning("anchor edit failed for %s", binding.session_id)

    async def _notify(
        self, binding: Binding, session: Session, notif: Notification
    ) -> None:
        thread = await self._thread(binding)
        if thread is None:
            return
        # The turn ended (or the session died) — the live progress message is
        # stale now; drop it so the thread reads as a clean transcript.
        if notif.kind in ("complete", "input", "turn_end", "error"):
            await self.progress.done(binding)
        # No mention => no push notification: routine transitions stay readable
        # in-channel without buzzing the phone.
        mentions = (
            mention_for(binding.spawned_by, self.settings.allowed_user_id_set)
            + " "
            if notif.mention
            else ""
        )
        text = mentions + notif.text
        if notif.kind == "complete":
            embed = completion_embed(
                session, fallback_title=binding.title, model=binding.model,
                spawned_by=binding.spawned_by,
            )
            pr_row = await self._completion_pr(binding)
            if pr_row is not None:
                await self._add_diffstat(pr_row, embed)
            diff_file, too_big = await self._pr_diff_file(pr_row)
            if too_big:
                text += " (diff too large for mobile — open the PR to review)"
            # Completion-card buttons: Post-review when this was a review
            # session (the deliberate publish step), Continue→ when a chain
            # phase is human-gated (advance() here decides the view only;
            # _advance_chain right after re-evaluates and acts on it).
            chain_next = ""
            if (
                binding.chain
                and binding.chain.get("halted") is None
                and self._component_handler is not None
            ):
                prs = await self.db.prs_for_session(binding.session_id)
                d = advance(binding.chain, session, pr_count=len(prs))
                if isinstance(d, Ask):
                    chain_next = d.phase.name
            can_post_review = bool(
                binding.review_of and self.github is not None
            )
            handler = self._component_handler
            done_view = (
                CompletionView(
                    binding.session_id,
                    handler,
                    review_of=binding.review_of if can_post_review else "",
                    chain_next_label=chain_next,
                )
                if handler is not None and (can_post_review or chain_next)
                else discord.utils.MISSING
            )
            await thread.send(
                text,
                embed=embed,
                file=diff_file if diff_file is not None else discord.utils.MISSING,
                view=done_view,
            )
            # Persist the summary for /digest — the tick-end upsert_binding
            # carries it to the db, so a digest stays one local read.
            summary = (session.structured_output or {}).get("summary")
            if isinstance(summary, str) and summary.strip():
                binding.summary = summary.strip()[:2000]
            await self._harvest_repo_notes(binding, session)
        else:
            await thread.send(text)

    async def _harvest_repo_notes(
        self, binding: Binding, session: Session
    ) -> None:
        """Completion write-back: structured_output.repo_notes → repo_notes
        rows the next spawn for this repo injects into its prompt. The
        UNIQUE(repo, note) constraint makes harvest idempotent; non-string
        or empty entries are dropped. First repo wins on multi-repo
        sessions — it's where the work presumptively happened."""
        so = session.structured_output or {}
        raw = so.get("repo_notes") or []
        repo = binding.repos.split(",")[0] if binding.repos else ""
        if not raw or not repo:
            return
        saved = 0
        for n in raw[:8]:
            if not isinstance(n, str) or not n.strip():
                continue
            if await self.db.add_note(
                repo, n.strip()[:500], f"devin:{binding.session_id}"
            ):
                saved += 1
        if not saved:
            return
        thread = await self._thread(binding)
        if thread is not None:
            await thread.send(
                f"📝 Saved {saved} repo note{'s' if saved != 1 else ''} "
                "— future sessions on this repo will see them."
            )

    async def _maybe_respawn(
        self, binding: Binding, session: Session
    ) -> bool:
        """An errored session respawns ONCE as a continuation in a fresh
        thread — seeded with the parent's structured_output summary + error
        detail so the new session continues the work instead of starting
        cold. `continued_from` on the child prevents unbounded chains.

        Returns True when a continuation spawned — the caller uses it to
        decide whether a chain binding still needs a halt notice."""
        if not self.settings.auto_respawn or binding.continued_from:
            return False
        so = session.structured_output or {}
        summary = so.get("summary") or binding.last_msg or "(none captured)"
        files = so.get("files_changed") or []
        prompt = (
            "A previous Devin session errored mid-task — continue its work.\n\n"
            f"Previous session: {binding.url or binding.session_id}\n"
            f"Progress summary: {summary}\n"
        )
        if files:
            prompt += f"Files touched: {', '.join(files[:20])}\n"
        if session.status_detail:
            prompt += f"Error detail: {session.status_detail}\n"
        prompt += (
            "\nInspect the repo state first to see what already landed, "
            "then carry the task forward."
        )
        try:
            _, thread = await spawn_session(
                cast("DevinMobileBot", self.bot),  # the real bot, not Client
                prompt=prompt,
                repos=binding.repos.split(",") if binding.repos else None,
                model=binding.model or None,
                title=f"{binding.title or 'session'} (continued)",
                continued_from=binding.session_id,
                # roll the dead session's burn into the chain budget
                chain=continued_chain(binding.chain, session),
                # nobody initiated the respawn — the owner stays the owner
                spawned_by=binding.spawned_by,
            )
        except SpawnError as e:
            log.warning("auto-respawn of %s failed: %s", binding.session_id, e)
            return False
        old_thread = await self._thread(binding)
        if old_thread is not None:
            await old_thread.send(
                "🔁 This session errored — respawned as a continuation in "
                f"{thread.mention}."
            )
        await thread.send(
            f"↩️ Continuing {binding.url or binding.session_id} after its error."
        )
        return True

    async def _note_chain_halt(self, binding: Binding) -> None:
        """A chain phase errored and nothing respawned it (continuations
        cap respawns at depth 1) — the chain can't advance on its own.
        Say so, and point at /continue: it carries the chain into a fresh
        binding so the failed phase gets a manual retry."""
        chain = binding.chain or {}
        phases = PLAYBOOKS.get(str(chain.get("playbook") or ""), [])
        step = int(chain.get("step") or 0)
        name = phases[step].name if step < len(phases) else "?"
        thread = await self._thread(binding)
        if thread is not None:
            await thread.send(
                f"⛓️ chain **{chain.get('playbook')}** halted — phase "
                f"`{name}` errored. `/continue` in this thread retries it."
            )

    async def _presence(self, n: int) -> None:
        """Bot status = number of sessions we're actively tracking —
        glanceable answer to 'is Devin doing anything right now'."""
        if n == self._last_presence:
            return
        if not self.bot.is_ready():
            return  # ws doesn't exist yet — next tick retries
        self._last_presence = n
        try:
            await self.bot.change_presence(
                activity=(
                    discord.Activity(
                        type=discord.ActivityType.watching,
                        name=f"{n} Devin session{'s' if n != 1 else ''}",
                    )
                    if n
                    else None  # idle — don't advertise a zero
                )
            )
        except Exception:  # noqa: BLE001 — presence is cosmetic
            log.debug("presence update failed", exc_info=True)

    async def _completion_pr(self, binding: Binding) -> PrRow | None:
        """The PR to summarize on completion — only when the session produced
        exactly one (ambiguity on multi-PR sessions isn't worth guessing)."""
        if self.github is None:
            return None
        prs = await self.db.prs_for_session(binding.session_id)
        if len(prs) != 1:
            return None
        row = prs[0]
        return row if (row.owner and row.repo and row.number) else None

    async def _pr_diff_file(
        self, row: PrRow | None
    ) -> tuple[discord.File | None, bool]:
        """Fetch the PR's combined diff as a Discord file — reading the
        actual change on a phone beats an embed field. Returns (file,
        too_big) — too_big lets the caller say why no file arrived."""
        if row is None or self.github is None:
            return None, False
        try:
            diff = await self.github.get_pr_diff(
                PullRef(owner=row.owner, repo=row.repo, number=row.number)
            )
        except Exception:  # noqa: BLE001 — the file is decoration
            log.debug("diff fetch failed for %s", row.pr_url)
            return None, False
        if diff is None:
            return None, True
        return (
            discord.File(
                io.BytesIO(diff.encode()),
                filename=f"{row.repo}-{row.number}.diff",
            ),
            False,
        )

    async def _add_diffstat(self, row: PrRow, embed: discord.Embed) -> None:
        """Append a GitHub-sourced +x/−y diffstat — the question 'how big
        was the change' always follows 'session finished' on a phone."""
        if self.github is None:
            return
        try:
            files = await self.github.get_pr_files(
                PullRef(owner=row.owner, repo=row.repo, number=row.number)
            )
        except Exception:  # noqa: BLE001 — diffstat is decoration
            return
        if not files:
            return
        adds = sum(f["additions"] for f in files)
        dels = sum(f["deletions"] for f in files)
        top = ", ".join(f"`{f['filename']}`" for f in files[:4])
        embed.add_field(
            name="Diff",
            value=f"+{adds} −{dels} across {len(files)} file(s): {top}"
                  + (" …" if len(files) > 4 else ""),
            inline=False,
        )

    # ---- playbook chains ---------------------------------------------------

    async def _advance_chain(
        self, binding: Binding, session: Session, *, force: bool = False
    ) -> discord.Thread | None:
        """Advance a completed phase's chain — returns the spawned phase's
        thread (for the chain_next handler) or None on Halt/Ask/action.

        The completed binding keeps its own `step`; the spawned child
        carries `step=next_idx` + the rolled-up `spent`. `action` phases
        consume instantly inside the loop (step advances past them so a
        resume never re-runs them). Idempotent across a crash between
        'decided' and 'spawned' via _resume_chains + child_of."""
        chain = dict(binding.chain or {})
        phases = PLAYBOOKS.get(str(chain.get("playbook") or ""), [])
        if not phases:
            return None
        if chain.get("halted") is not None:
            return None  # terminal — only /continue (a fresh phase) revives it
        if chain.get("pending") is not None and not force:
            return None  # a Continue→ (or spawn-retry) button is already posted
        prs = await self.db.prs_for_session(binding.session_id)
        spawned_thread: discord.Thread | None = None
        while True:
            decision = advance(chain, session, pr_count=len(prs))
            if isinstance(decision, Halt):
                chain["step"] = len(phases)  # terminal marker
                chain["halted"] = decision.reason
                chain["pending"] = None
                binding.chain = dict(chain)
                await self.db.upsert_binding(binding)
                thread = await self._thread(binding)
                if thread is not None:
                    await thread.send(
                        f"⛓️ chain **{chain.get('playbook')}** done — "
                        f"{decision.reason}"
                    )
                return spawned_thread
            nxt = decision.phase
            # single_pr gate passed — bank the produced PR's key so later
            # phases (review_of, arm_automerge) can find it
            if nxt.gate == "single_pr" and len(prs) == 1:
                row = prs[0]
                if row.owner and row.repo and row.number:
                    chain["pr_key"] = f"{row.owner}/{row.repo}#{row.number}"
            if isinstance(decision, Ask) and not force:
                chain["pending"] = decision.next_idx
                binding.chain = dict(chain)
                await self.db.upsert_binding(binding)
                return spawned_thread
            if nxt.kind == "action":
                chain["step"] = decision.next_idx
                chain["pending"] = None
                binding.chain = dict(chain)
                await self.db.upsert_binding(binding)
                await self._run_chain_action(binding, nxt, chain)
                # a forced tap authorizes ONE gated phase, not every gate
                # downstream of an action hop
                force = False
                continue
            chain["pending"] = None
            binding.chain = dict(chain)
            await self.db.upsert_binding(binding)
            try:
                spawned_thread = await self._spawn_chain_phase(
                    binding, session, nxt, chain, decision.next_idx, len(phases)
                )
            except Exception:
                # Park the chain on the failed phase: pending + a retry
                # button beats silently waiting for the restart sweep.
                chain["pending"] = decision.next_idx
                binding.chain = dict(chain)
                await self.db.upsert_binding(binding)
                log.exception(
                    "chain phase spawn failed for %s", binding.session_id
                )
                try:
                    thread = await self._thread(binding)
                    if thread is not None:
                        await thread.send(
                            f"⛓️ `{nxt.name}` failed to spawn — tap to retry.",
                            view=(
                                CompletionView(
                                    binding.session_id,
                                    self._component_handler,
                                    chain_next_label=nxt.name,
                                )
                                if self._component_handler is not None
                                else discord.utils.MISSING
                            ),
                        )
                except Exception:  # noqa: BLE001 — the retry note is best-effort
                    pass
                raise
            return spawned_thread

    async def _spawn_chain_phase(
        self,
        binding: Binding,
        session: Session,
        phase: Phase,
        chain: dict,
        next_idx: int,
        total: int,
    ) -> discord.Thread | None:
        """Spawn the next chain phase as a continued_from child seeded
        with the completed session's structured output."""
        so = session.structured_output or {}
        pr_key = str(chain.get("pr_key") or "")
        ctx = {
            "orig": str(chain.get("orig") or ""),
            "summary": str(
                so.get("summary") or binding.last_msg or "(no summary captured)"
            ),
            "files": ", ".join(
                str(f) for f in (so.get("files_changed") or [])[:20]
            ),
            "notes": str(so.get("notes") or ""),
            "pr_key": pr_key,
            "pr_url": (
                f"https://github.com/{pr_key.replace('#', '/pull/')}"
                if pr_key else ""
            ),
            "prev_url": session.url or binding.url or "",
            "repo": binding.repos or "",
        }
        prompt = render_prompt(phase, ctx)
        spent = round(float(chain.get("spent") or 0) + session.acus_consumed, 4)
        cap = chain.get("cap")
        budgets = [
            b for b in (
                (float(cap) - spent) if cap else None,
                self.settings.max_acu_limit,
            )
            if b is not None
        ]
        # remaining can dip to ~0 on a forced advance past cap — floor at 1
        # so the child still gets a meaningful (not uncapped) budget
        budget = max(min(budgets), 1) if budgets else None
        new_chain = {
            **chain, "step": next_idx, "spent": spent, "pending": None,
        }
        _, child_thread = await spawn_session(
            cast("DevinMobileBot", self.bot),
            prompt=prompt,
            repos=binding.repos.split(",") if binding.repos else None,
            model=binding.model or None,
            # chain.title is the /chain base — binding.title would accrete
            # a " · phase" suffix every hop
            title=f"{chain.get('title') or binding.title or chain.get('playbook')}"
                  f" · {phase.name}",
            budget=budget,
            continued_from=binding.session_id,
            review_of=pr_key if phase.review else "",
            chain=new_chain,
            # a Continue→ tap doesn't re-own the chain — inherit the
            # original spawner so pings keep going to whoever launched it
            spawned_by=binding.spawned_by,
        )
        old_thread = await self._thread(binding)
        if old_thread is not None:
            await old_thread.send(
                f"⛓️ `{phase.name}` phase spawned → {child_thread.mention}"
            )
        await child_thread.send(
            f"⛓️ chain **{chain.get('playbook')}** · phase {next_idx + 1}/"
            f"{total} `{phase.name}` — continuing "
            f"{binding.url or binding.session_id}"
        )
        return child_thread

    async def _run_chain_action(
        self, binding: Binding, phase: Phase, chain: dict
    ) -> None:
        """Bot-side chain step — resolves instantly, no session spawned."""
        thread = await self._thread(binding)
        if phase.action == "arm_automerge":
            pr_key = str(chain.get("pr_key") or "")
            ref = parse_issue_ref(pr_key) if pr_key else None
            found = (
                await self.db.binding_for_pr(*ref) if ref else None
            )
            if found is None:
                if thread is not None:
                    await thread.send(
                        f"⛓️ couldn't arm auto-merge — no tracked PR "
                        f"{pr_key or 'recorded'}."
                    )
                return
            _, row = found
            if row.state in ("merged", "closed"):
                if thread is not None:
                    await thread.send(
                        f"⛓️ {pr_key} already {row.state} — nothing to merge."
                    )
                return
            row.auto_merge = True
            await self.db.upsert_pr(row)
            # The PR belongs to an earlier phase's session — resurrect that
            # binding's polling so _poll_pr can fire the merge on green CI.
            prod = await self.db.get_binding(row.session_id)
            if prod is not None and not prod.active:
                prod.active = True
                await self.db.upsert_binding(prod)
            if thread is not None:
                await thread.send(
                    f"⛓️ auto-merge armed on {pr_key} — merges when CI "
                    "goes green."
                )

    async def _resume_chains(self) -> None:
        """Startup sweep: completed chain phases whose next phase never
        spawned re-run the advance. `child_of` is the dedupe — a child
        spawned just before a crash makes the re-run a no-op."""
        for b in await self.db.chain_resumable():
            chain = b.chain or {}
            phases = PLAYBOOKS.get(str(chain.get("playbook") or ""), [])
            if chain.get("halted") is not None:
                continue  # terminal-marked
            if chain.get("pending") is not None:
                continue  # a Continue→ (or spawn-retry) button is already posted
            if int(chain.get("step") or 0) + 1 >= len(phases):
                continue  # ran off the end
            if await self.db.child_of(b.session_id) is not None:
                continue  # the next phase already spawned pre-crash
            try:
                session = await self.devin.get_session(b.session_id)
                await self._advance_chain(b, session)
            except Exception:
                log.exception("chain resume failed for %s", b.session_id)

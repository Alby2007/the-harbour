import asyncio
import io
import json
import logging
import random
import re
import time
from dataclasses import dataclass

import discord

from .acp_bridge import SessionStream
from .config import Settings
from .db import Binding, Database, PrRow
from .devin_client import DevinClient
from .embeds import completion_embed, pr_embed, status_embed
from .github_client import GithubClient, PullRef, parse_pr_url
from .models import Session, SessionMessage
from .progress import ProgressTracker, chunk_text_from, summarize_update
from .views import ComponentHandler, FixCIView, PRView

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
        while True:
            try:
                for binding in await self.db.active_bindings():
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
            binding.status, binding.status_detail, session, binding.last_msg
        )
        had_activity = had_activity or notif is not None
        if had_activity:
            binding.last_activity_at = int(time.time())
            binding.quiet_alerted = False
        binding.acus = session.acus_consumed
        await self._check_acu(binding, session)
        if session.title:
            binding.title = session.title
        binding.status = session.status
        binding.status_detail = session.status_detail
        binding.url = session.url
        is_complete = notif is not None and notif.kind == "complete"
        await self._update_anchor(binding, session, complete=is_complete)
        if notif:
            await self._notify(binding, session, notif)
        await self._watchdog(binding, session)
        # Derived from the live status so a stale write can't strand a
        # reactivated thread in the parked state.
        binding.active = session.status not in QUIET_STATUSES
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
        line = summarize_update(update)
        if line is None:
            return
        binding = await self.db.get_binding(session_id)
        if binding is None:
            return
        await self.progress.push(binding, line)

    # ---- budget + liveness ------------------------------------------------

    async def _check_acu(self, binding: Binding, session: Session) -> None:
        """Ping once when ACU burn crosses 80%/100% of the configured cap —
        the cap being hit mid-task is exactly when a phone ping matters."""
        cap = self.settings.max_acu_limit
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
                        f"{cap} ACUs consumed.",
                    ),
                )

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
        mention = " ".join(f"<@{u}>" for u in self.settings.allowed_user_id_set)
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
            for i, chunk in enumerate(chunks[:8]):
                suffix = "\n… *(truncated — see session)*" if i == 7 and len(chunks) > 8 else ""
                await thread.send(chunk + suffix)
            for url in attachments:
                name = url.rsplit("/", 1)[-1] or "attachment"
                await thread.send(f"Attachment: [{name}]({url})")
            return
        for i, chunk in enumerate(chunks[:8]):
            suffix = "\n… *(truncated — see session)*" if i == 7 and len(chunks) > 8 else ""
            await thread.send(chunk + suffix)
        for url in attachments:
            name = url.rsplit("/", 1)[-1] or "attachment"
            await thread.send(f"Attachment: [{name}]({url})")

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
                    session, fallback_title=binding.title, model=binding.model
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
            " ".join(f"<@{u}>" for u in self.settings.allowed_user_id_set) + " "
            if notif.mention
            else ""
        )
        text = mentions + notif.text
        if notif.kind == "complete":
            embed = completion_embed(
                session, fallback_title=binding.title, model=binding.model
            )
            pr_row = await self._completion_pr(binding)
            if pr_row is not None:
                await self._add_diffstat(pr_row, embed)
            diff_file, too_big = await self._pr_diff_file(pr_row)
            if too_big:
                text += " (diff too large for mobile — open the PR to review)"
            await thread.send(
                text,
                embed=embed,
                file=diff_file if diff_file is not None else discord.utils.MISSING,
            )
        else:
            await thread.send(text)

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

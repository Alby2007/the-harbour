import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass

import discord

from .config import Settings
from .db import Binding, Database
from .devin_client import DevinClient
from .embeds import completion_embed, status_embed
from .models import Session, SessionMessage

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


def relayable(items: list[SessionMessage], seen: set[str]) -> list[SessionMessage]:
    out = []
    for m in items:
        if m.event_id in seen:
            continue
        seen.add(m.event_id)
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
        self, bot: discord.Client, devin: DevinClient, db: Database, settings: Settings
    ) -> None:
        self.bot = bot
        self.devin = devin
        self.db = db
        self.settings = settings

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
        cursor = binding.msg_cursor
        last_devin: str | None = None
        while True:
            page = await self.devin.list_messages(binding.session_id, after=cursor)
            for m in relayable(page.items, binding.seen_event_ids):
                clean, _ = extract_attachments(m.message or "")
                if clean:
                    last_devin = clean
                await self._relay_message(binding, m)
            if page.end_cursor:
                cursor = page.end_cursor
            if not page.has_next_page or not page.items:
                break
        binding.msg_cursor = cursor

        session = await self.devin.get_session(binding.session_id)
        notif = classify_transition(
            binding.status, binding.status_detail, session, last_devin
        )
        if session.title:
            binding.title = session.title
        binding.status = session.status
        binding.status_detail = session.status_detail
        binding.url = session.url
        is_complete = notif is not None and notif.kind == "complete"
        await self._update_anchor(binding, session, complete=is_complete)
        if notif:
            await self._notify(binding, session, notif)
        if session.status in QUIET_STATUSES:
            binding.active = False
        await self.db.upsert_binding(binding)

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
        text, attachments = extract_attachments(m.message or "")
        chunks = chunk_text(text) if text else []
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
        # No mention => no push notification: routine transitions stay readable
        # in-channel without buzzing the phone.
        mentions = (
            " ".join(f"<@{u}>" for u in self.settings.allowed_user_id_set) + " "
            if notif.mention
            else ""
        )
        text = mentions + notif.text
        if notif.kind == "complete":
            await thread.send(
                text,
                embed=completion_embed(
                    session, fallback_title=binding.title, model=binding.model
                ),
            )
        else:
            await thread.send(text)

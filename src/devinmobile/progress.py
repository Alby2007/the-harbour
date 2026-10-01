"""Live turn progress — renders ACP ``session/update`` notifications into a
single Discord message per session that gets edited in place.

One message per turn (not one per tool call) keeps threads readable; edits
are throttled to ~3s. The message is deleted on turn end — the transcript
lives in the v3-relayed chat messages, this is just a liveness display.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from .db import Binding

log = logging.getLogger(__name__)

MIN_EDIT_INTERVAL = 3.0
REPLY_EDIT_INTERVAL = 1.2  # reply streaming is the content — snappier
REPLY_MSG_LIMIT = 1900  # preview caps at one message; canonical text replaces it
MAX_LINES = 6

GetThread = Callable[["Binding"], Awaitable["discord.Thread | None"]]


def summarize_update(update: dict[str, Any]) -> str | None:
    """Map a session/update payload to one short line, or None to skip.

    ``tool_call`` carries a human-readable ``title`` ("Read /path/x.py",
    "Run `pytest`") — that's the progress signal. Thought/message chunks
    duplicate what the v3 relay already posts and would double-display.
    """
    kind = update.get("sessionUpdate")
    if kind == "tool_call":
        title = (update.get("title") or "").strip()
        if not title:
            raw = update.get("rawInput") or {}
            title = str(raw.get("command") or raw.get("path") or "working")
        return f"`{title[:110]}`"
    if kind == "tool_call_update" and update.get("status") == "failed":
        return "a step failed — retrying"
    return None


def chunk_text_from(update: dict[str, Any]) -> str:
    """Extract text from an ``agent_message_chunk`` update.

    Per ACP, ``content`` is ``{"type": "text", "text": …}``; the probe also
    observed list-shaped content blocks in the wild — handle both.
    """
    c = update.get("content")
    if isinstance(c, dict):
        return (c.get("text") or "") if c.get("type") == "text" else ""
    if isinstance(c, list):
        return "".join(
            b.get("text", "")
            for b in c
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _norm(s: str) -> str:
    return " ".join(s.split())


THINKING_TEXT = "⏳ *Devin is thinking…*"


@dataclass
class _Stream:
    msg_id: int | None = None
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    last_edit: float = 0.0
    flush_task: asyncio.Task | None = None
    dirty: bool = False


@dataclass
class _Reply:
    """One streamed agent message within a turn — accumulates chunks and
    live-edits a single Discord message. Sealed when a tool call follows
    (text separated by tool calls is almost always a different message);
    the next chunk then opens a fresh _Reply."""
    buf: str = ""
    msg_id: int | None = None
    sealed: bool = False
    dirty: bool = False
    last_edit: float = 0.0
    flush_task: asyncio.Task | None = None


class ProgressTracker:
    def __init__(self, get_thread: GetThread) -> None:
        self._get_thread = get_thread
        self._streams: dict[str, _Stream] = {}
        self._replies: dict[str, list[_Reply]] = {}

    async def thinking(self, binding: Binding) -> None:
        """Post the pre-work placeholder right after a user send — covers the
        send→first-tool-call dead air. The first push() morphs it into the
        Working list; a relayed reply or turn end deletes it."""
        # a new turn starts — any streamed replies still unmatched are stale
        # previews whose canonical copies already posted (or never will)
        for seg in self._replies.pop(binding.session_id, []):
            await self._drop_reply(binding, seg)
        st = self._streams.setdefault(binding.session_id, _Stream())
        if st.lines:
            return  # already mid-turn — the Working display is showing
        st.dirty = True
        await self._flush(binding, st)

    async def clear_thinking(self, binding: Binding) -> None:
        """A real Devin chat message arrived — drop the placeholder, but only
        if no tool lines have streamed yet (mid-turn relayed messages must
        not kill an active Working list)."""
        st = self._streams.get(binding.session_id)
        if st is not None and not st.lines:
            await self.done(binding)

    async def push(self, binding: Binding, line: str) -> None:
        st = self._streams.setdefault(binding.session_id, _Stream())
        # a tool call after reply text means a new message is coming —
        # seal the current reply so the next chunk opens a fresh one
        segs = self._replies.get(binding.session_id)
        if segs and segs[-1].buf and not segs[-1].sealed:
            segs[-1].sealed = True
            await self._flush_reply(binding, segs[-1])
        if st.lines and st.lines[-1] == line:
            return  # consecutive dupes (retried reads etc.) add nothing
        was_idle = not st.lines
        st.lines.append(line)
        st.dirty = True
        # the thinking→working morph is the one transition that must not
        # wait for the throttle — it's the user-visible "it started" signal
        if was_idle or time.time() - st.last_edit >= MIN_EDIT_INTERVAL:
            await self._flush(binding, st)
        elif st.flush_task is None or st.flush_task.done():
            st.flush_task = asyncio.create_task(self._flush_later(binding, st))

    async def _flush_later(self, binding: Binding, st: _Stream) -> None:
        try:
            await asyncio.sleep(MIN_EDIT_INTERVAL)
            await self._flush(binding, st)
        except asyncio.CancelledError:
            pass

    async def _flush(self, binding: Binding, st: _Stream) -> None:
        if not st.dirty:
            return
        st.dirty = False
        st.last_edit = time.time()
        thread = await self._get_thread(binding)
        if thread is None:
            return
        text = (
            "**Working…**\n" + "\n".join(st.lines) if st.lines else THINKING_TEXT
        )
        try:
            if st.msg_id is None:
                msg = await thread.send(text)
                st.msg_id = msg.id
            else:
                msg = await thread.fetch_message(st.msg_id)
                await msg.edit(content=text[:1900])
        except discord.HTTPException:
            st.msg_id = None  # message gone — next flush reposts

    # ---- streamed reply text ----------------------------------------------

    async def stream_chunk(self, binding: Binding, text: str) -> None:
        """Accumulate an agent_message_chunk into the live reply message."""
        if not text:
            return
        segs = self._replies.setdefault(binding.session_id, [])
        seg = segs[-1] if segs and not segs[-1].sealed else None
        if seg is None:
            seg = _Reply()
            segs.append(seg)
            # A bare thinking placeholder promotes into the reply — it must
            # leave _Stream ownership or done()/clear_thinking would delete
            # a message that now carries real reply text.
            st = self._streams.get(binding.session_id)
            if st is not None and st.msg_id is not None and not st.lines:
                seg.msg_id = st.msg_id
                self._streams.pop(binding.session_id)
        seg.buf += text
        seg.dirty = True
        if time.time() - seg.last_edit >= REPLY_EDIT_INTERVAL:
            await self._flush_reply(binding, seg)
        elif seg.flush_task is None or seg.flush_task.done():
            seg.flush_task = asyncio.create_task(
                self._flush_reply_later(binding, seg)
            )

    async def _flush_reply_later(self, binding: Binding, seg: _Reply) -> None:
        try:
            await asyncio.sleep(REPLY_EDIT_INTERVAL)
            await self._flush_reply(binding, seg)
        except asyncio.CancelledError:
            pass

    async def _flush_reply(self, binding: Binding, seg: _Reply) -> None:
        if not seg.dirty:
            return
        seg.dirty = False
        seg.last_edit = time.time()
        thread = await self._get_thread(binding)
        if thread is None:
            return
        visible = seg.buf[:REPLY_MSG_LIMIT]
        if len(seg.buf) > REPLY_MSG_LIMIT:
            visible += "\n… *(streaming — see session for the rest)*"
        try:
            if seg.msg_id is None:
                msg = await thread.send(visible)
                seg.msg_id = msg.id
            else:
                msg = await thread.fetch_message(seg.msg_id)
                await msg.edit(content=visible)
        except discord.HTTPException:
            seg.msg_id = None  # gone — next flush reposts

    async def reconcile(self, binding: Binding, clean: str) -> bool:
        """A canonical v3 message arrived. If it matches a streamed reply,
        post canonical text normally and drop the preview messages — returns
        True so the caller skips its own post. A full mismatch means it was
        a different message in a multi-message turn → False, post as usual."""
        segs = self._replies.get(binding.session_id)
        if not segs:
            return False
        nclean = _norm(clean)
        for seg in segs:
            nbuf = _norm(seg.buf)
            if nbuf and (nclean.startswith(nbuf) or nbuf.startswith(nclean)):
                await self._drop_reply(binding, seg)
                segs.remove(seg)
                return True
        return False

    async def _drop_reply(self, binding: Binding, seg: _Reply) -> None:
        if seg.flush_task:
            seg.flush_task.cancel()
        if seg.msg_id is not None:
            try:
                thread = await self._get_thread(binding)
                if thread is not None:
                    msg = await thread.fetch_message(seg.msg_id)
                    await msg.delete()
            except discord.HTTPException:
                pass
            seg.msg_id = None

    async def done(self, binding: Binding) -> None:
        """Turn ended — drop the progress message (chat keeps the record).
        Streamed replies stay AND stay matchable: the canonical v3 copy
        often lands a poll or two after the turn ends — reconcile() needs
        the segs around to replace the preview instead of duplicating it.
        Leftovers are dropped on the next turn's thinking()."""
        for seg in self._replies.get(binding.session_id, []):
            if seg.flush_task:
                seg.flush_task.cancel()
            seg.sealed = True  # next turn's chunks open a fresh reply
            if seg.dirty:
                await self._flush_reply(binding, seg)  # don't freeze mid-word
        st = self._streams.pop(binding.session_id, None)
        if st is None:
            return
        if st.flush_task:
            st.flush_task.cancel()
        if st.msg_id is not None:
            try:
                thread = await self._get_thread(binding)
                if thread is not None:
                    msg = await thread.fetch_message(st.msg_id)
                    await msg.delete()
            except discord.HTTPException:
                pass

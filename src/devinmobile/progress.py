"""Live turn progress — renders ACP ``session/update`` notifications into a
single Discord message per session that gets edited in place.

One message per turn (not one per tool call) keeps threads readable; edits
are throttled to ~3s. The message is deleted on turn end — the transcript
lives in the v3-relayed chat messages, this is just a liveness display.
"""

from __future__ import annotations

import asyncio
import logging
import re
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


@dataclass
class _Line:
    """One rendered row in the Working message. ``key`` is the ACP
    ``toolCallId`` — later updates for the same call patch this line in
    place instead of appending a new one."""
    text: str
    key: str = ""


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _tool_text(update: dict[str, Any]) -> str:
    """The tool call's base label — empty string when nothing is
    renderable. ``kind == "execute"`` gets ``$ cmd`` shell styling."""
    title = (update.get("title") or "").strip()
    raw = update.get("rawInput") or {}
    if not title:
        title = str(raw.get("command") or raw.get("path") or "")
    if not title:
        locs = update.get("locations") or []
        if locs and isinstance(locs[0], dict):
            title = str(locs[0].get("path") or "")
    if not title:
        return ""
    if update.get("kind") == "execute":
        cmd = str(raw.get("command") or title)
        cmd = cmd.removeprefix("Run ").removeprefix("run ")
        return f"$ `{cmd[:110]}`"
    return f"`{title[:110]}`"


def _failure_tail(update: dict[str, Any]) -> str | None:
    """Last meaningful output line of a failed call — 'what did it say' is
    the first question a phone user asks after seeing ✗."""
    text = ""
    for block in update.get("content") or []:
        if not isinstance(block, dict):
            continue
        inner = block.get("content")
        if isinstance(inner, dict) and inner.get("type") == "text":
            text = str(inner.get("text") or "")
        elif block.get("type") == "terminal":
            text = str(block.get("text") or block.get("output") or text)
    raw_out = update.get("rawOutput")
    if isinstance(raw_out, str) and raw_out:
        text = raw_out
    elif isinstance(raw_out, dict):
        for k in ("output", "text", "stderr", "error", "message"):
            if raw_out.get(k):
                text = str(raw_out[k])
    for ln in reversed(_ANSI.sub("", text).splitlines()):
        if ln.strip():
            return f"↳ `{ln.strip()[:140]}`"
    return None


def summarize_update(update: dict[str, Any]) -> list[_Line]:
    """Map a session/update payload to Working-message lines ([] = skip).

    ACP upserts tool calls by ``toolCallId`` — we surface that as a _Line
    key so ``push`` patches the status onto the same line. Thought/message
    chunks duplicate the v3 relay and stay skipped.
    """
    kind = update.get("sessionUpdate")
    if kind not in ("tool_call", "tool_call_update"):
        return []
    call_id = str(update.get("toolCallId") or "")
    status = update.get("status") or ""
    base = _tool_text(update)
    if not base:
        if kind == "tool_call_update":
            return []  # bare status flip on an unseen call — nothing to say
        base = "`working`"
    if status == "completed":
        base += " ✓"
    elif status == "failed":
        base += " ✗"
    lines = [_Line(text=base, key=call_id)]
    if status == "failed":
        tail = _failure_tail(update)
        if tail:
            lines.append(
                _Line(text=tail, key=f"{call_id}:out" if call_id else "")
            )
    return lines


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
    lines: deque[_Line] = field(
        default_factory=lambda: deque(maxlen=MAX_LINES)
    )
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

    async def push(self, binding: Binding, line: _Line | str) -> None:
        if isinstance(line, str):
            line = _Line(text=line)
        st = self._streams.setdefault(binding.session_id, _Stream())
        # a tool call after reply text means a new message is coming —
        # seal the current reply so the next chunk opens a fresh one
        segs = self._replies.get(binding.session_id)
        if segs and segs[-1].buf and not segs[-1].sealed:
            segs[-1].sealed = True
            await self._flush_reply(binding, segs[-1])
        # ACP upserts by toolCallId: a status update rewrites its line
        # (adds ✓/✗) instead of stacking a new one
        if line.key:
            for existing in st.lines:
                if existing.key == line.key:
                    if existing.text == line.text:
                        return
                    existing.text = line.text
                    st.dirty = True
                    if time.time() - st.last_edit >= MIN_EDIT_INTERVAL:
                        await self._flush(binding, st)
                    elif st.flush_task is None or st.flush_task.done():
                        st.flush_task = asyncio.create_task(
                            self._flush_later(binding, st)
                        )
                    return
        if st.lines and st.lines[-1].text == line.text:
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
            "**Working…**\n" + "\n".join(li.text for li in st.lines)
            if st.lines
            else THINKING_TEXT
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

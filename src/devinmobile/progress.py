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


@dataclass
class _Stream:
    msg_id: int | None = None
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    last_edit: float = 0.0
    flush_task: asyncio.Task | None = None
    dirty: bool = False


class ProgressTracker:
    def __init__(self, get_thread: GetThread) -> None:
        self._get_thread = get_thread
        self._streams: dict[str, _Stream] = {}

    async def push(self, binding: Binding, line: str) -> None:
        st = self._streams.setdefault(binding.session_id, _Stream())
        if st.lines and st.lines[-1] == line:
            return  # consecutive dupes (retried reads etc.) add nothing
        st.lines.append(line)
        st.dirty = True
        if time.time() - st.last_edit >= MIN_EDIT_INTERVAL:
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
        text = "**Working…**\n" + "\n".join(st.lines)
        try:
            if st.msg_id is None:
                msg = await thread.send(text)
                st.msg_id = msg.id
            else:
                msg = await thread.fetch_message(st.msg_id)
                await msg.edit(content=text[:1900])
        except discord.HTTPException:
            st.msg_id = None  # message gone — next flush reposts

    async def done(self, binding: Binding) -> None:
        """Turn ended — drop the progress message (chat keeps the record)."""
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

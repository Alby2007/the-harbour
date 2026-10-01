"""Live progress rendering: update summarization + throttled message edits."""

import asyncio

from devinmobile.db import Binding
from devinmobile.progress import ProgressTracker, summarize_update


def test_summarize_update():
    assert summarize_update({"sessionUpdate": "tool_call",
                             "title": "Read /x.py"}) == "`Read /x.py`"
    assert summarize_update({"sessionUpdate": "tool_call"}) is not None
    # chat/thought chunks are the v3 relay's job — don't double-display
    assert summarize_update({"sessionUpdate": "agent_message_chunk"}) is None
    assert summarize_update({"sessionUpdate": "session_info_update"}) is None
    assert summarize_update({"sessionUpdate": "tool_call_update",
                             "status": "failed"}) == "a step failed — retrying"


class _Msg:
    def __init__(self, mid):
        self.id = mid
        self.edits: list[str] = []
        self.deleted = False

    async def edit(self, *, content: str, **kw):
        self.edits.append(content)

    async def delete(self):
        self.deleted = True


class _Thread:
    def __init__(self):
        self.messages: dict[int, _Msg] = {}
        self._next = 1

    async def send(self, text, **kw):
        m = _Msg(self._next)
        self._next += 1
        self.messages[m.id] = m
        return m

    async def fetch_message(self, mid):
        return self.messages[mid]


async def test_tracker_edits_one_message_and_cleans_up():
    thread = _Thread()
    tracker = ProgressTracker(lambda b: asyncio.sleep(0, thread))
    b = Binding(session_id="s1", thread_id=1, channel_id=1)

    await tracker.push(b, "`ls -la`")
    assert len(thread.messages) == 1
    msg = next(iter(thread.messages.values()))

    # rapid pushes collapse into edits, not new messages
    for line in ("`cat a`", "`cat b`"):
        await tracker.push(b, line)
    assert len(thread.messages) == 1
    # let the debounce task flush
    await asyncio.sleep(3.2)
    assert msg.edits and "`cat b`" in msg.edits[-1]

    await tracker.done(b)
    assert msg.deleted


async def test_consecutive_dupe_lines_dropped():
    thread = _Thread()
    tracker = ProgressTracker(lambda b: asyncio.sleep(0, thread))
    b = Binding(session_id="s2", thread_id=1, channel_id=1)
    await tracker.push(b, "`x`")
    await tracker.push(b, "`x`")
    st = tracker._streams["s2"]
    assert list(st.lines) == ["`x`"]

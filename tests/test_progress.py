"""Live progress rendering: update summarization + throttled message edits."""

import asyncio

from devinmobile.db import Binding
from devinmobile.progress import (
    ProgressTracker,
    chunk_text_from,
    summarize_update,
)


def test_summarize_update():
    lines = summarize_update({"sessionUpdate": "tool_call",
                              "title": "Read /x.py", "toolCallId": "t1"})
    assert [li.text for li in lines] == ["`Read /x.py`"]
    assert lines[0].key == "t1"
    # no title/id at all still renders a placeholder line for a NEW call
    assert summarize_update({"sessionUpdate": "tool_call"})
    # chat/thought chunks are the v3 relay's job — don't double-display
    assert summarize_update({"sessionUpdate": "agent_message_chunk"}) == []
    assert summarize_update({"sessionUpdate": "session_info_update"}) == []
    # a bare status flip with no title/id is noise, not a line
    assert summarize_update({"sessionUpdate": "tool_call_update",
                             "status": "completed"}) == []


def test_summarize_execute_renders_shell_prompt():
    lines = summarize_update({
        "sessionUpdate": "tool_call", "toolCallId": "c1",
        "kind": "execute", "rawInput": {"command": "pytest -x"},
    })
    assert lines[0].text == "$ `pytest -x`"
    # title-only fallback drops Devin's "Run " verb
    lines = summarize_update({
        "sessionUpdate": "tool_call", "kind": "execute",
        "title": "Run pytest -x",
    })
    assert lines[0].text == "$ `pytest -x`"


def test_summarize_status_marks():
    done = summarize_update({"sessionUpdate": "tool_call_update",
                             "toolCallId": "c1", "title": "pytest",
                             "status": "completed"})
    assert done[0].text == "`pytest` ✓"
    failed = summarize_update({"sessionUpdate": "tool_call_update",
                               "toolCallId": "c1", "title": "pytest",
                               "status": "failed",
                               "rawOutput": "ok\n\n5 failed, 2 passed"})
    assert failed[0].text == "`pytest` ✗"
    assert failed[1].text == "↳ `5 failed, 2 passed`"
    assert failed[1].key == "c1:out"


def test_failure_tail_from_content_blocks_and_ansi():
    upd = {"sessionUpdate": "tool_call_update", "toolCallId": "c1",
           "title": "pytest", "status": "failed",
           "content": [{"type": "content",
                        "content": {"type": "text",
                                    "text": "\x1b[31mFAILED a.py::t\x1b[0m"}}]}
    lines = summarize_update(upd)
    assert lines[1].text == "↳ `FAILED a.py::t`"
    # no output anywhere → just the ✗ line
    no_out = summarize_update({"sessionUpdate": "tool_call_update",
                               "toolCallId": "c1", "title": "pytest",
                               "status": "failed"})
    assert [li.text for li in no_out] == ["`pytest` ✗"]


class _Msg:
    def __init__(self, mid):
        self.id = mid
        self.content: str = ""
        self.edits: list[str] = []
        self.deleted = False

    async def edit(self, *, content: str, **kw):
        self.edits.append(content)
        self.content = content

    async def delete(self):
        self.deleted = True


class _Thread:
    def __init__(self):
        self.messages: dict[int, _Msg] = {}
        self._next = 1

    async def send(self, text, **kw):
        m = _Msg(self._next)
        m.content = text
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
    assert [li.text for li in st.lines] == ["`x`"]


async def test_tool_update_patches_line_in_place():
    """toolCallId-keyed upserts: a completed status rewrites the existing
    line (adds ✓) rather than stacking a second line."""
    thread = _Thread()
    tracker = ProgressTracker(lambda b: asyncio.sleep(0, thread))
    b = Binding(session_id="s5", thread_id=1, channel_id=1)
    from devinmobile.progress import _Line
    await tracker.push(b, _Line(text="$ `pytest -x`", key="c1"))
    await tracker.push(b, _Line(text="$ `pytest -x` ✓", key="c1"))
    st = tracker._streams["s5"]
    assert [li.text for li in st.lines] == ["$ `pytest -x` ✓"]
    # a different call appends normally
    await tracker.push(b, _Line(text="`Read x.py`", key="c2"))
    assert len(st.lines) == 2
    # patching an evicted/unknown key just appends
    await tracker.push(b, _Line(text="`grep foo`", key="c9"))
    assert len(st.lines) == 3
    await tracker.done(b)  # cancels the deferred flush task


async def test_thinking_placeholder_morphs_and_clears():
    thread = _Thread()
    tracker = ProgressTracker(lambda b: asyncio.sleep(0, thread))
    b = Binding(session_id="s3", thread_id=1, channel_id=1)

    await tracker.thinking(b)
    assert len(thread.messages) == 1
    msg = next(iter(thread.messages.values()))

    # thinking is idempotent while idle — same message, no repost
    await tracker.thinking(b)
    assert len(thread.messages) == 1

    # first tool call morphs the placeholder into the Working list
    await tracker.push(b, "`ls -la`")
    assert len(thread.messages) == 1
    assert msg.edits and "Working" in msg.edits[-1]

    # a relayed Devin message must NOT clear an active Working list
    await tracker.clear_thinking(b)
    assert not msg.deleted
    await tracker.done(b)
    assert msg.deleted


async def test_thinking_cleared_by_first_reply():
    thread = _Thread()
    tracker = ProgressTracker(lambda b: asyncio.sleep(0, thread))
    b = Binding(session_id="s4", thread_id=1, channel_id=1)
    await tracker.thinking(b)
    msg = next(iter(thread.messages.values()))
    await tracker.clear_thinking(b)  # reply landed before any tool call
    assert msg.deleted


# ---- streamed reply text --------------------------------------------------


def test_chunk_text_from_shapes():
    assert chunk_text_from(
        {"sessionUpdate": "agent_message_chunk",
         "content": {"type": "text", "text": "hi"}}
    ) == "hi"
    assert chunk_text_from(
        {"sessionUpdate": "agent_message_chunk",
         "content": [{"type": "text", "text": "a"},
                     {"type": "image"},  # ignored
                     {"type": "text", "text": "b"}]}
    ) == "ab"
    assert chunk_text_from({"sessionUpdate": "agent_message_chunk"}) == ""


async def test_stream_chunk_edits_one_message():
    thread = _Thread()
    tracker = ProgressTracker(lambda b: asyncio.sleep(0, thread))
    b = Binding(session_id="r1", thread_id=1, channel_id=1)
    await tracker.stream_chunk(b, "Hello ")
    await tracker.stream_chunk(b, "world")  # inside the 1.2s throttle
    assert len(thread.messages) == 1
    await asyncio.sleep(1.4)  # let the deferred flush land
    msg = next(iter(thread.messages.values()))
    assert msg.content == "Hello world"


async def test_thinking_promotes_into_reply_and_survives_done():
    thread = _Thread()
    tracker = ProgressTracker(lambda b: asyncio.sleep(0, thread))
    b = Binding(session_id="r2", thread_id=1, channel_id=1)
    await tracker.thinking(b)
    msg = next(iter(thread.messages.values()))
    await tracker.stream_chunk(b, "I'm on it")
    # same message, now carrying real text
    assert len(thread.messages) == 1
    assert msg.content == "I'm on it"
    await tracker.done(b)
    assert not msg.deleted  # promoted placeholders are never deleted


async def test_tool_line_seals_reply_and_next_chunk_opens_new():
    thread = _Thread()
    tracker = ProgressTracker(lambda b: asyncio.sleep(0, thread))
    b = Binding(session_id="r3", thread_id=1, channel_id=1)
    await tracker.stream_chunk(b, "first reply")
    await tracker.push(b, "`run pytest`")  # seals the reply
    await tracker.stream_chunk(b, "second message")
    assert len(thread.messages) == 3  # reply1 + working + reply2
    segs = tracker._replies["r3"]
    assert len(segs) == 2 and segs[0].sealed and not segs[1].sealed


async def test_reconcile_replaces_preview():
    thread = _Thread()
    tracker = ProgressTracker(lambda b: asyncio.sleep(0, thread))
    b = Binding(session_id="r4", thread_id=1, channel_id=1)
    await tracker.stream_chunk(b, "I found two config files — which")
    msg = next(iter(thread.messages.values()))
    # canonical text lands via the v3 poll: longer than the partial stream
    assert await tracker.reconcile(
        b, "I found two config files — which one should I use?"
    )
    assert msg.deleted
    assert not tracker._replies["r4"]  # seg consumed


async def test_reconcile_mismatch_keeps_preview():
    thread = _Thread()
    tracker = ProgressTracker(lambda b: asyncio.sleep(0, thread))
    b = Binding(session_id="r5", thread_id=1, channel_id=1)
    await tracker.stream_chunk(b, "half-written buffer")
    assert not await tracker.reconcile(b, "a totally different message")
    assert not next(iter(thread.messages.values())).deleted


async def test_done_seals_but_keeps_replies_matchable():
    thread = _Thread()
    tracker = ProgressTracker(lambda b: asyncio.sleep(0, thread))
    b = Binding(session_id="r6", thread_id=1, channel_id=1)
    await tracker.stream_chunk(b, "final answer")
    await tracker.done(b)
    # canonical may land a poll after turn end — still reconciles
    assert await tracker.reconcile(b, "final answer")
    # a new turn drops unreconciled leftovers
    await tracker.stream_chunk(b, "next turn text")
    await tracker.thinking(b)
    assert not tracker._replies.get("r6")

from devinmobile.models import Session, SessionMessage
from devinmobile.relay import (
    chunk_text,
    classify_transition,
    extract_attachments,
    relayable,
)


def make_msg(event_id: str, source: str = "devin", text: str = "x") -> SessionMessage:
    return SessionMessage(event_id=event_id, source=source, message=text, created_at=1)


def make_session(status: str, detail: str | None = None) -> Session:
    return Session(session_id="devin-x", url="https://x", status=status, status_detail=detail)


def test_chunk_short():
    assert chunk_text("hello") == ["hello"]


def test_chunk_splits_on_newline():
    text = ("a" * 1000) + "\n" + ("b" * 1000) + "\n" + ("c" * 500)
    chunks = chunk_text(text, limit=1000)
    assert all(len(c) <= 1100 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_chunk_hard_cut_no_newlines():
    chunks = chunk_text("x" * 4000, limit=1900)
    assert len(chunks) == 3
    assert "".join(chunks) == "x" * 4000


def test_relayable_filters_user_and_dedupes():
    seen = {"e0"}
    items = [
        make_msg("e0", "devin"),          # already seen
        make_msg("e1", "user"),           # our own forwarded message
        make_msg("e2", "devin"),          # new devin message
        make_msg("e3", "devin"),
    ]
    out = relayable(items, seen)
    assert [m.event_id for m in out] == ["e2", "e3"]
    assert seen == {"e0", "e1", "e2", "e3"}


def test_transition_exit_notifies_complete():
    n = classify_transition("running", "working", make_session("exit", "finished"))
    assert n and n.kind == "complete"


def test_transition_waiting_for_user_plain_turn_end():
    # task done, no question asked -> "turn ended", not "asking for input"
    n = classify_transition(
        "running", "working", make_session("running", "waiting_for_user"),
        last_devin_msg="Created and ran the script successfully.",
    )
    assert n and n.kind == "turn_end" and n.mention
    assert "finished its turn" in n.text


def test_transition_waiting_for_user_question():
    n = classify_transition(
        "running", "working", make_session("running", "waiting_for_user"),
        last_devin_msg="I found two config files — which one should I use?",
    )
    assert n and n.kind == "input" and n.mention
    assert "which one should I use?" in n.text


def test_transition_waiting_for_user_no_message():
    n = classify_transition("running", "working", make_session("running", "waiting_for_user"))
    assert n and n.kind == "turn_end"


def test_transition_waiting_for_approval():
    n = classify_transition("running", "working", make_session("running", "waiting_for_approval"))
    assert n and n.kind == "approval" and n.mention


def test_transition_suspended_inactivity_is_quiet():
    n = classify_transition("running", "waiting_for_user", make_session("suspended", "inactivity"))
    assert n and n.kind == "suspended" and not n.mention


def test_transition_suspended_carries_reason():
    n = classify_transition("running", "working", make_session("suspended", "out_of_credits"))
    assert n and n.kind == "suspended" and n.mention and "out_of_credits" in n.text


def test_no_repeat_ping_when_detail_unchanged():
    sess = make_session("running", "waiting_for_user")
    assert classify_transition("running", "waiting_for_user", sess) is None


def test_first_poll_no_notify_for_steady_state():
    assert classify_transition(None, None, make_session("running", "working")) is None


def test_error_transition():
    n = classify_transition("running", "working", make_session("error"))
    assert n and n.kind == "error"


def test_extract_attachments():
    text = (
        'Done.\n\nATTACHMENT:{"url":"https://app.devin.ai/attachments/u-u/fib.py",'
        '"fileSize":177}'
    )
    clean, urls = extract_attachments(text)
    assert clean == "Done."
    assert urls == ["https://app.devin.ai/attachments/u-u/fib.py"]


def test_extract_attachments_plain_text_unchanged():
    clean, urls = extract_attachments("no attachments here")
    assert clean == "no attachments here"
    assert urls == []

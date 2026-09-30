from devinmobile.embeds import completion_embed, status_embed
from devinmobile.models import PullRequest, Session


def make_session(**kw) -> Session:
    base = dict(session_id="devin-x", url="https://app.devin.ai/sessions/devin-x", status="running")
    base.update(kw)
    return Session(**base)


def test_status_embed_fields():
    e = status_embed(
        make_session(
            status="running",
            status_detail="waiting_for_user",
            title="fix tests",
            devin_mode="lite",
            acus_consumed=2.5,
        )
    )
    assert e.title == "fix tests"
    assert "waiting_for_user" in e.fields[0].value
    assert e.color.value == 0x2B6CB0


def test_completion_embed_structured_output():
    e = completion_embed(
        make_session(
            status="exit",
            status_detail="finished",
            title="done",
            structured_output={
                "summary": "did the thing",
                "files_changed": [f"file{i}.py" for i in range(20)],
                "tests_passed": True,
                "notes": "watch out",
            },
            pull_requests=[PullRequest(pr_url="https://github.com/o/r/pull/9", pr_state="open")],
        )
    )
    assert e.description == "did the thing"
    names = [f.name for f in e.fields]
    assert "Files changed" in names and "Tests" in names and "Notes" in names
    files_field = next(f for f in e.fields if f.name == "Files changed")
    assert "… and 5 more" in files_field.value
    assert any("pull/9" in f.value for f in e.fields)


def test_embed_falls_back_to_session_id_title():
    e = status_embed(make_session(title=None))
    assert e.title == "devin-x"

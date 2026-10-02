from devinmobile.embeds import (
    completion_embed,
    mention_for,
    spawned_by_label,
    status_embed,
)
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


def test_mention_for_owner_vs_infra():
    allowed = frozenset({1, 2})
    # a Discord-user spawner gets the ping alone — the whole allowlist
    # only joins for marker/legacy spawnings nobody owns
    assert mention_for("1", allowed) == "<@1>"
    assert mention_for("github", allowed) == "<@1> <@2>"
    assert mention_for("intake", allowed) == "<@1> <@2>"
    assert mention_for("", allowed) == "<@1> <@2>"
    # a digit spawned_by that ISN'T an allowlisted id — e.g. a caller-
    # supplied /task `by:` — can't redirect pings at a stranger
    assert mention_for("999", allowed) == "<@1> <@2>"


def test_spawned_by_label():
    assert spawned_by_label("42") == "<@42>"
    assert spawned_by_label("github") == "github"
    assert spawned_by_label("") == ""


def test_status_embed_by_field():
    e = status_embed(make_session(), spawned_by="42")
    assert next(f for f in e.fields if f.name == "By").value == "<@42>"
    e = status_embed(make_session(), spawned_by="github")
    assert next(f for f in e.fields if f.name == "By").value == "github"
    e = status_embed(make_session())  # unset → no field at all
    assert all(f.name != "By" for f in e.fields)

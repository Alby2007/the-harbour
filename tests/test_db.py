import time

from devinmobile.db import Binding, Database, PrRow, ScheduleRow


async def test_roundtrip(tmp_path):
    db = await Database.connect(str(tmp_path / "t.db"))
    b = Binding(
        session_id="devin-1",
        thread_id=111,
        channel_id=222,
        anchor_msg_id=333,
        title="t",
        url="https://u",
        status="running",
        status_detail="working",
        msg_cursor="cur",
        seen_event_ids=["e1", "e2"],
    )
    await db.upsert_binding(b)

    by_thread = await db.get_binding_by_thread(111)
    assert by_thread is not None
    assert by_thread.session_id == "devin-1"
    assert by_thread.seen_event_ids == ["e1", "e2"]

    by_sid = await db.get_binding("devin-1")
    assert by_sid is not None and by_sid.active

    b.active = False
    await db.upsert_binding(b)
    assert await db.active_bindings() == []
    assert (await db.get_binding("devin-1")).active is False

    await db.close()


async def test_multiple_bindings_listed(tmp_path):
    db = await Database.connect(str(tmp_path / "t.db"))
    for i in range(3):
        await db.upsert_binding(
            Binding(session_id=f"devin-{i}", thread_id=100 + i, channel_id=1)
        )
    all_b = await db.all_bindings()
    assert len(all_b) == 3
    act = await db.active_bindings()
    assert len(act) == 3
    await db.close()


async def test_binding_repos_roundtrip_and_backfill(tmp_path):
    path = str(tmp_path / "t.db")
    db = await Database.connect(path)
    # a session that produced a PR but predates the repos column
    await db.upsert_binding(
        Binding(session_id="old", thread_id=1, channel_id=1)
    )
    await db.upsert_pr(PrRow(
        session_id="old", pr_url="https://github.com/o/r/pull/1",
        owner="o", repo="r", number=1,
    ))
    # a spawn-time attribution + a repo-less session
    await db.upsert_binding(Binding(
        session_id="new", thread_id=2, channel_id=1, repos="a/b,c/d",
    ))
    await db.upsert_binding(Binding(session_id="none", thread_id=3, channel_id=1))
    await db.close()

    db = await Database.connect(path)  # reconnect → migration + backfill run
    assert (await db.get_binding("new")).repos == "a/b,c/d"
    assert (await db.get_binding("old")).repos == "o/r"  # backfilled
    assert (await db.get_binding("none")).repos == ""
    # a state-only upsert (repos="") must not wipe the attribution
    b = await db.get_binding("new")
    b.repos = ""
    await db.upsert_binding(b)
    assert (await db.get_binding("new")).repos == "a/b,c/d"
    await db.close()


async def test_binding_new_fields_roundtrip(tmp_path):
    db = await Database.connect(str(tmp_path / "t.db"))
    await db.upsert_binding(Binding(
        session_id="s1", thread_id=1, channel_id=1,
        acus=3.5, acu_warned=1, last_activity_at=12345, quiet_alerted=True,
    ))
    b = await db.get_binding("s1")
    assert b.acus == 3.5 and b.acu_warned == 1
    assert b.last_activity_at == 12345 and b.quiet_alerted is True
    await db.close()


async def test_schedules_roundtrip(tmp_path):
    db = await Database.connect(str(tmp_path / "t.db"))
    now = int(time.time())
    sid = await db.add_schedule(ScheduleRow(
        id=0, prompt="audit deps", repos=["o/r"], model="swe2-max",
        interval_seconds=3600, next_run_at=now - 5,
    ))
    assert sid > 0
    due = await db.due_schedules(now)
    assert len(due) == 1 and due[0].prompt == "audit deps"
    assert due[0].repos == ["o/r"] and due[0].model == "swe2-max"

    await db.schedule_ran(sid, "sess1", now)
    due = await db.due_schedules(now)
    assert not due  # pushed an hour forward, not due again
    row = (await db.all_schedules())[0]
    assert row.next_run_at == now + 3600 and row.last_session_id == "sess1"

    assert await db.delete_schedule(sid)
    assert not await db.all_schedules()
    assert not await db.delete_schedule(sid)  # already gone
    await db.close()


async def test_pr_auto_merge_flag_survives_state_upserts(tmp_path):
    from devinmobile.db import PrRow

    db = await Database.connect(str(tmp_path / "t.db"))
    await db.upsert_pr(PrRow(
        session_id="s1", pr_url="https://github.com/o/r/pull/1",
        owner="o", repo="r", number=1, state="open", auto_merge=True,
    ))
    # a state-only poll upsert carries auto_merge=None — must not clear the flag
    await db.upsert_pr(PrRow(
        session_id="s1", pr_url="https://github.com/o/r/pull/1",
        state="open", checks_state="success",
    ))
    row = await db.get_pr("s1", "https://github.com/o/r/pull/1")
    assert row.auto_merge is True
    await db.close()


async def test_repo_notes_roundtrip_and_dedupe(tmp_path):
    db = await Database.connect(str(tmp_path / "t.db"))
    assert await db.add_note("o/r", "tests are flaky — pytest -x", "u1")
    # UNIQUE(repo, note): the same pair can't be saved twice
    assert await db.add_note("o/r", "tests are flaky — pytest -x", "u2") is False
    assert await db.add_note("o/r", "different note", "u1")
    await db.add_note("O/Other", "case probe", None)
    # case-insensitive repo match, newest first
    got = await db.notes_for_repos(["O/R"])
    assert got == {"o/r": ["different note", "tests are flaky — pytest -x"]}
    got = await db.notes_for_repos(["o/other"])
    assert got == {"O/Other": ["case probe"]}
    assert await db.notes_for_repos([]) == {}
    # list + delete
    rows = await db.list_notes()
    assert len(rows) == 3
    rows = await db.list_notes("o/R")
    assert len(rows) == 2
    nid = rows[0][0]
    assert await db.delete_note(nid)
    assert not await db.delete_note(nid)


async def test_repo_notes_eight_per_repo_cap(tmp_path):
    db = await Database.connect(str(tmp_path / "t.db"))
    for i in range(10):
        await db.add_note("o/r", f"note {i}", None)
    got = await db.notes_for_repos(["o/r"])
    assert len(got["o/r"]) == 8
    assert got["o/r"][0] == "note 9"  # newest first

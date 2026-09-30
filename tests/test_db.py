from devinmobile.db import Binding, Database


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
        seen_event_ids={"e1", "e2"},
    )
    await db.upsert_binding(b)

    by_thread = await db.get_binding_by_thread(111)
    assert by_thread is not None
    assert by_thread.session_id == "devin-1"
    assert by_thread.seen_event_ids == {"e1", "e2"}

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

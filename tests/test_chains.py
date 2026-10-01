"""Playbook chains — gate eval, _advance_chain, resume sweep, db roundtrip."""

import asyncio

import pytest

import devinmobile.relay as relay_mod
from devinmobile.bot.main import DevinMobileBot
from devinmobile.chains import (
    PLAYBOOKS,
    Advance,
    Ask,
    Halt,
    advance,
    continued_chain,
    render_prompt,
)
from devinmobile.db import Binding, Database, PrRow
from devinmobile.models import Session
from devinmobile.relay import Relay
from devinmobile.spawn import SpawnError


class _Settings:
    allowed_user_id_set = {1}
    max_acu_limit = 25
    auto_respawn = True
    silence_alert_minutes = 0
    create_as_user_id = None
    github_merge_method = "squash"


class _Chan:
    def __init__(self):
        self.sent: list[str] = []

    @property
    def mention(self):
        return "<#99>"

    async def send(self, text, **kw):
        self.sent.append(text)


class _Bot:
    def __init__(self):
        self.settings = _Settings()

    def get_channel(self, _id):
        return None

    async def fetch_channel(self, _id):
        return None

    def is_ready(self):
        return True


def _binding(**kw) -> Binding:
    kw.setdefault("session_id", "s1")
    kw.setdefault("thread_id", 10)
    kw.setdefault("channel_id", 5)
    kw.setdefault("anchor_msg_id", 100)
    return Binding(**kw)


def _chain(**kw) -> dict:
    return {
        "playbook": "janitor",
        "step": 0,
        "pending": None,
        "cap": 10.0,
        "spent": 0.0,
        "pr_key": "",
        "orig": "clean the repo",
        "auto": False,
        **kw,
    }


def _sess(sid: str = "s1", **kw) -> Session:
    kw.setdefault("status", "exit")
    return Session(session_id=sid, url=f"https://x/{sid}", **kw)


async def _db(tmp_path) -> Database:
    return await Database.connect(str(tmp_path / "t.db"))


def _relay(bot, db) -> Relay:
    return Relay(bot, None, db, _Settings())  # type: ignore[arg-type]


# ---- advance() gate evaluation -----------------------------------------------


def test_terminal_step_halts():
    d = advance(_chain(step=3), _sess(), pr_count=0)  # janitor has 4 phases
    assert isinstance(d, Halt) and "complete" in d.reason


def test_proceed_false_halts():
    sess = _sess(structured_output={"proceed": False, "summary": "clean"})
    d = advance(_chain(), sess, pr_count=0)
    assert isinstance(d, Halt) and "nothing to do" in d.reason


def test_proceed_false_skips_phase():
    # iterate's review reports clean → apply is SKIPPED, not halted:
    # the scan continues to the automerge action so the PR still merges
    d = advance(
        _chain(playbook="iterate", step=1),
        _sess(structured_output={"proceed": False, "summary": "clean"}),
        pr_count=1,
    )
    assert isinstance(d, Advance) and d.phase.name == "automerge"
    assert d.next_idx == 3


def test_skip_reason_beats_later_gate_failure():
    # janitor audit reports clean → fix skipped, review's single_pr gate
    # then has nothing to pass on — the reported reason stays the real one
    d = advance(
        _chain(step=0),
        _sess(structured_output={"proceed": False}),
        pr_count=0,
    )
    assert isinstance(d, Halt) and "audit" in d.reason


def test_continued_chain_strips_markers_and_rolls_spent():
    c = _chain(pending=2, spent=1.0)
    c["halted"] = "chain budget spent"
    out = continued_chain(c, _sess(acus_consumed=2.5))
    assert out is not None
    assert out["pending"] is None and "halted" not in out
    assert out["spent"] == 3.5  # dead session's burn counts toward the cap
    assert continued_chain(None, _sess()) is None


def test_proceed_absent_continues_to_ask():
    # janitor fix is auto=False → human gate
    d = advance(_chain(), _sess(), pr_count=0)
    assert isinstance(d, Ask) and d.phase.name == "fix" and d.next_idx == 1


def test_chain_auto_flips_ask_to_advance():
    d = advance(_chain(auto=True), _sess(), pr_count=0)
    assert isinstance(d, Advance) and d.phase.name == "fix"


def test_single_pr_gate():
    # step=1 (fix completed) → next=review with gate=single_pr (auto)
    d = advance(_chain(step=1), _sess(), pr_count=1)
    assert isinstance(d, Advance) and d.phase.name == "review"
    d = advance(_chain(step=1), _sess(), pr_count=0)
    assert isinstance(d, Halt) and "PR" in d.reason
    d = advance(_chain(step=1), _sess(), pr_count=2)
    assert isinstance(d, Halt)


def test_budget_cap_halts():
    d = advance(
        _chain(spent=1.0, cap=10.0), _sess(acus_consumed=9.5), pr_count=0
    )
    assert isinstance(d, Halt) and "budget" in d.reason


def test_unknown_playbook_halts():
    d = advance(_chain(playbook="nope"), _sess(), pr_count=0)
    assert isinstance(d, Halt)


def test_render_prompt_substitutes_and_blanks_missing():
    out = render_prompt(
        PLAYBOOKS["janitor"][1],
        {"summary": "found stuff", "prev_url": "u1", "files": "a.py"},
    )
    assert "found stuff" in out and "u1" in out and "{" not in out


# ---- _advance_chain -----------------------------------------------------------


async def test_ask_sets_pending_no_spawn(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    monkeypatch.setattr(
        relay_mod, "spawn_session",
        lambda *a, **k: pytest.fail("should not spawn"),
    )
    b = _binding(chain=_chain())
    await db.upsert_binding(b)
    r = _relay(_Bot(), db)
    out = await r._advance_chain(b, _sess(acus_consumed=2.0))
    assert out is None
    stored = await db.get_binding("s1")
    assert stored is not None and stored.chain["pending"] == 1


async def test_advance_auto_spawns_child(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    calls = []

    async def fake_spawn(bot, **kw):
        calls.append(kw)
        return _sess("s2"), _Chan()

    monkeypatch.setattr(relay_mod, "spawn_session", fake_spawn)
    b = _binding(
        chain=_chain(auto=True), repos="o/r", model="SWE-2 Max", title="t"
    )
    await db.upsert_binding(b)
    r = _relay(_Bot(), db)
    sess = _sess(acus_consumed=2.0, structured_output={"summary": "found x"})
    out = await r._advance_chain(b, sess)
    assert out is not None  # the spawned phase's thread
    kw = calls[0]
    assert kw["continued_from"] == "s1"
    assert kw["repos"] == ["o/r"]
    assert kw["model"] == "SWE-2 Max"
    assert kw["chain"]["step"] == 1 and kw["chain"]["spent"] == 2.0
    assert "found x" in kw["prompt"]
    # budget = min(global 25, cap 10 − spent 2) = 8
    assert kw["budget"] == 8.0


async def test_advance_halt_marks_terminal(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    monkeypatch.setattr(
        relay_mod, "spawn_session",
        lambda *a, **k: pytest.fail("should not spawn"),
    )
    b = _binding(chain=_chain(step=3))
    await db.upsert_binding(b)
    r = _relay(_Bot(), db)
    chan = _Chan()

    async def _t(_b):
        return chan

    r._thread = _t  # shadow the isinstance-checked method
    await r._advance_chain(b, _sess())
    stored = await db.get_binding("s1")
    assert stored is not None and stored.chain["step"] == 4
    assert stored.chain["halted"] == "chain complete"
    assert any("chain" in s and "done" in s for s in chan.sent)


async def test_halted_chain_does_not_advance(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    monkeypatch.setattr(
        relay_mod, "spawn_session",
        lambda *a, **k: pytest.fail("should not spawn"),
    )
    b = _binding(chain=_chain(halted="chain budget spent"))
    await db.upsert_binding(b)
    r = _relay(_Bot(), db)
    assert await r._advance_chain(b, _sess()) is None


async def test_iterate_clean_review_skips_apply_and_arms(tmp_path, monkeypatch):
    # review (step 1) reports proceed=false → apply skipped, automerge
    # still arms the banked PR and the chain completes
    db = await _db(tmp_path)
    monkeypatch.setattr(
        relay_mod, "spawn_session",
        lambda *a, **k: pytest.fail("should not spawn"),
    )
    prod = _binding(session_id="s0")
    prod.active = False
    await db.upsert_binding(prod)
    await db.upsert_pr(PrRow(
        session_id="s0", pr_url="https://github.com/o/r/pull/7",
        owner="o", repo="r", number=7, state="open",
    ))
    b = _binding(session_id="s2", thread_id=11)
    b.chain = _chain(playbook="iterate", step=1, pr_key="o/r#7")
    await db.upsert_binding(b)
    r = _relay(_Bot(), db)
    chan = _Chan()

    async def _t(_b):
        return chan

    r._thread = _t
    await r._advance_chain(
        b, _sess("s2", structured_output={"proceed": False})
    )
    row = await db.get_pr("s0", "https://github.com/o/r/pull/7")
    assert row is not None and row.auto_merge is True
    assert (await db.get_binding("s0")).active is True  # producer resurrected
    stored = await db.get_binding("s2")
    assert stored is not None and stored.chain["halted"] == "chain complete"
    assert any("auto-merge armed" in s for s in chan.sent)


async def test_advance_spawn_failure_parks_pending(tmp_path, monkeypatch):
    # a spawn that blows up leaves the chain retryable — pending is set so
    # the posted Continue→ button doubles as the retry affordance
    db = await _db(tmp_path)

    async def boom(*a, **k):
        raise SpawnError("bridge down")

    monkeypatch.setattr(relay_mod, "spawn_session", boom)
    b = _binding(chain=_chain(auto=True))
    await db.upsert_binding(b)
    r = _relay(_Bot(), db)
    chan = _Chan()

    async def _t(_b):
        return chan

    r._thread = _t
    with pytest.raises(SpawnError):
        await r._advance_chain(b, _sess())
    stored = await db.get_binding("s1")
    assert stored is not None and stored.chain["pending"] == 1
    assert any("failed to spawn" in s for s in chan.sent)


async def test_action_phase_arms_automerge_then_completes(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    monkeypatch.setattr(
        relay_mod, "spawn_session",
        lambda *a, **k: pytest.fail("should not spawn"),
    )
    # the fix phase's session (exited, inactive) owns the PR row
    prod = _binding()
    prod.active = False
    await db.upsert_binding(prod)
    await db.upsert_pr(PrRow(
        session_id="s1", pr_url="https://github.com/o/r/pull/5",
        owner="o", repo="r", number=5, state="open",
    ))
    # the review session just completed, carrying the chain at step=2
    b2 = _binding(session_id="s2", thread_id=11)
    b2.chain = _chain(step=2, pr_key="o/r#5")
    await db.upsert_binding(b2)
    r = _relay(_Bot(), db)
    chan = _Chan()

    async def _t(_b):
        return chan

    r._thread = _t
    await r._advance_chain(b2, _sess("s2"))
    row = await db.get_pr("s1", "https://github.com/o/r/pull/5")
    assert row is not None and row.auto_merge is True
    # producer binding resurrected so _poll_pr can fire the merge
    prod2 = await db.get_binding("s1")
    assert prod2 is not None and prod2.active is True
    # action consumed → next_idx ran off the end → chain completed
    stored = await db.get_binding("s2")
    assert stored is not None and stored.chain["step"] == 4
    assert any("auto-merge armed" in s for s in chan.sent)


async def test_pending_without_force_is_noop(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    monkeypatch.setattr(
        relay_mod, "spawn_session",
        lambda *a, **k: pytest.fail("should not spawn"),
    )
    b = _binding(chain=_chain(pending=1))
    await db.upsert_binding(b)
    r = _relay(_Bot(), db)
    await r._advance_chain(b, _sess())
    stored = await db.get_binding("s1")
    assert stored is not None and stored.chain["pending"] == 1


async def test_force_advance_past_pending_spawns(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    calls = []

    async def fake_spawn(bot, **kw):
        calls.append(kw)
        return _sess("s2"), _Chan()

    monkeypatch.setattr(relay_mod, "spawn_session", fake_spawn)
    b = _binding(chain=_chain(pending=1))
    await db.upsert_binding(b)
    r = _relay(_Bot(), db)
    out = await r._advance_chain(b, _sess(), force=True)
    assert out is not None and calls[0]["chain"]["step"] == 1
    stored = await db.get_binding("s1")
    assert stored is not None and stored.chain["pending"] is None


# ---- _resume_chains ------------------------------------------------------------


class _Devin:
    def __init__(self, session):
        self._session = session

    async def get_session(self, sid):
        return self._session


async def test_resume_advances_orphaned_parent(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    spawned = []

    async def fake_spawn(bot, **kw):
        spawned.append(kw)
        return _sess("s9"), _Chan()

    monkeypatch.setattr(relay_mod, "spawn_session", fake_spawn)
    parent = _binding(chain=_chain(auto=True), status="exit")
    await db.upsert_binding(parent)
    sess = _sess("s1", structured_output={"summary": "found stuff"})
    r = Relay(_Bot(), _Devin(sess), db, _Settings())  # type: ignore[arg-type]
    await r._resume_chains()
    assert spawned and spawned[0]["continued_from"] == "s1"
    assert spawned[0]["chain"]["step"] == 1


async def test_resume_skips_when_child_exists(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    monkeypatch.setattr(
        relay_mod, "spawn_session",
        lambda *a, **k: pytest.fail("should not spawn"),
    )
    await db.upsert_binding(_binding(chain=_chain(), status="exit"))
    await db.upsert_binding(
        _binding(session_id="s2", continued_from="s1", status="running")
    )
    r = Relay(_Bot(), _Devin(_sess()), db, _Settings())  # type: ignore[arg-type]
    await r._resume_chains()


async def test_resume_skips_pending_and_terminal(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    monkeypatch.setattr(
        relay_mod, "spawn_session",
        lambda *a, **k: pytest.fail("should not spawn"),
    )
    await db.upsert_binding(_binding(chain=_chain(pending=1), status="exit"))
    await db.upsert_binding(
        _binding(session_id="s3", chain=_chain(step=4), status="exit")
    )
    await db.upsert_binding(
        _binding(
            session_id="s4",
            chain=_chain(halted="chain budget spent"),
            status="exit",
        )
    )
    r = Relay(_Bot(), _Devin(_sess()), db, _Settings())  # type: ignore[arg-type]
    await r._resume_chains()


# ---- chain_next component handler ---------------------------------------------


class _Resp:
    def __init__(self):
        self.sent: list[str] = []
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, text, **kw):
        self.sent.append(text)
        self._done = True

    async def defer(self, **kw):
        self._done = True


class _Followup:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text, **kw):
        self.sent.append(text)


class _Ix:
    def __init__(self, uid=1, message=None):
        self.user = type("U", (), {"id": uid})()
        self.response = _Resp()
        self.followup = _Followup()
        self.message = message
        self.data = {}


class _Dev:
    async def get_session(self, sid):
        return _sess(sid)


class _AdvanceStub:
    """Stands in for the real Relay: holds the "advance" open briefly so a
    second tap lands mid-flight, then clears pending like the real
    _advance_chain does before spawning."""

    def __init__(self, db):
        self._db = db
        self._locks: dict[str, asyncio.Lock] = {}
        self.calls: list[tuple[str, bool]] = []

    def _lock(self, sid):
        return self._locks.setdefault(sid, asyncio.Lock())

    async def _advance_chain(self, b, s, *, force=False):
        self.calls.append((b.session_id, force))
        await asyncio.sleep(0.05)
        b.chain = {**(b.chain or {}), "pending": None}
        await self._db.upsert_binding(b)
        return None


async def test_chain_next_forces_pending_advance(tmp_path):
    db = await _db(tmp_path)
    await db.upsert_binding(
        _binding(chain=_chain(pending=1), status="exit")
    )
    bot = _Bot()
    bot.db = db
    bot.devin = _Dev()
    bot.relay = _AdvanceStub(db)
    bot.github = None
    ix = _Ix()
    await DevinMobileBot.handle_component(
        bot, ix, "chain_next", "s1", None  # type: ignore[arg-type]
    )
    assert bot.relay.calls == [("s1", True)]
    assert ix.followup.sent


async def test_chain_next_double_tap_spawns_once(tmp_path):
    # the pending check is re-done under the per-session lock — the losing
    # tap sees the consumed marker instead of spawning a second phase
    db = await _db(tmp_path)
    await db.upsert_binding(
        _binding(chain=_chain(pending=1), status="exit")
    )
    bot = _Bot()
    bot.db = db
    bot.devin = _Dev()
    bot.relay = _AdvanceStub(db)
    bot.github = None
    ix1, ix2 = _Ix(), _Ix()
    await asyncio.gather(
        DevinMobileBot.handle_component(
            bot, ix1, "chain_next", "s1", None  # type: ignore[arg-type]
        ),
        DevinMobileBot.handle_component(
            bot, ix2, "chain_next", "s1", None  # type: ignore[arg-type]
        ),
    )
    assert bot.relay.calls == [("s1", True)]
    msgs = (
        ix1.response.sent + ix1.followup.sent
        + ix2.response.sent + ix2.followup.sent
    )
    assert any("already moved on" in m for m in msgs)


async def test_chain_next_stale_chain_is_ephemeral(tmp_path):
    db = await _db(tmp_path)
    await db.upsert_binding(_binding(chain=_chain()))  # pending=None
    bot = _Bot()
    bot.db = db
    bot.devin = None
    ix = _Ix()
    await DevinMobileBot.handle_component(
        bot, ix, "chain_next", "s1", None  # type: ignore[arg-type]
    )
    assert any("already moved on" in m for m in ix.response.sent)


# ---- respawn + db ---------------------------------------------------------------


async def test_respawn_carries_chain(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    captured = {}

    async def fake_spawn(bot, **kw):
        captured.update(kw)
        return _sess("s2"), _Chan()

    monkeypatch.setattr(relay_mod, "spawn_session", fake_spawn)
    r = _relay(_Bot(), db)
    b = _binding(chain=_chain(spent=1.0))
    sess = _sess(status="error", status_detail="boom", acus_consumed=3.0)
    assert await r._maybe_respawn(b, sess) is True
    assert captured["chain"]["playbook"] == "janitor"
    # the failed phase's burn rolls into the chain budget
    assert captured["chain"]["spent"] == 4.0


async def test_respawn_refuses_chain_children(tmp_path, monkeypatch):
    # phases after the first are continued_from children — no auto-respawn,
    # so the caller can post the chain-halt notice instead
    db = await _db(tmp_path)
    monkeypatch.setattr(
        relay_mod, "spawn_session",
        lambda *a, **k: pytest.fail("should not spawn"),
    )
    r = _relay(_Bot(), db)
    b = _binding(chain=_chain(step=1), continued_from="s0")
    sess = _sess(status="error", status_detail="boom")
    assert await r._maybe_respawn(b, sess) is False


async def test_errored_chain_phase_posts_halt_note(tmp_path):
    db = await _db(tmp_path)
    b = _binding(chain=_chain(step=1))
    await db.upsert_binding(b)
    r = _relay(_Bot(), db)
    chan = _Chan()

    async def _t(_b):
        return chan

    r._thread = _t
    await r._note_chain_halt(b)
    assert any("halted" in s and "`fix`" in s for s in chan.sent)
    assert any("/continue" in s for s in chan.sent)


async def test_chain_roundtrip_and_helpers(tmp_path):
    db = await _db(tmp_path)
    await db.upsert_binding(_binding(chain=_chain(spent=1.5), status="exit"))
    b = await db.get_binding("s1")
    assert b is not None
    assert b.chain["playbook"] == "janitor" and b.chain["spent"] == 1.5
    # chain_resumable: chain + status="exit"
    resumable = await db.chain_resumable()
    assert {x.session_id for x in resumable} == {"s1"}
    # a state-only upsert must not wipe the chain
    await db.upsert_binding(_binding(status="running"))
    b = await db.get_binding("s1")
    assert b is not None and b.chain["playbook"] == "janitor"
    assert await db.chain_resumable() == []  # running again — not resumable
    # child_of
    await db.upsert_binding(_binding(session_id="s2", continued_from="s1"))
    child = await db.child_of("s1")
    assert child is not None and child.session_id == "s2"
    assert await db.child_of("s2") is None
    # chained_bindings sees both chain rows
    await db.upsert_binding(_binding(session_id="s4", chain=_chain()))
    assert len(await db.chained_bindings()) == 2


async def test_has_armed_open_pr(tmp_path):
    db = await _db(tmp_path)
    await db.upsert_binding(_binding())
    assert await db.has_armed_open_pr("s1") is False
    await db.upsert_pr(PrRow(
        session_id="s1", pr_url="u1", state="open", auto_merge=True,
    ))
    assert await db.has_armed_open_pr("s1") is True
    await db.upsert_pr(PrRow(
        session_id="s1", pr_url="u1", state="merged", auto_merge=True,
    ))
    assert await db.has_armed_open_pr("s1") is False

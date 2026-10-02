"""Emoji-reaction commands + reply-quoting — tested via unbound methods
on a stub bot with a real Database."""

from devinmobile.bot.main import DevinMobileBot
from devinmobile.db import Binding, Database, PrRow


class _Chan:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text, **kw):
        self.sent.append(text)


class _Reaction:
    def __init__(self, emoji: str, me: bool = False):
        self.emoji = emoji
        self.me = me


class _Msg:
    def __init__(self, mid, content="", reactions=()):
        self.id = mid
        self.content = content
        self.reactions = list(reactions)
        self.channel = _Chan()
        self.added: list[str] = []
        self.removed: list[str] = []
        self.embeds = []
        self.reference = None
        self.components = []
        self.edits: list[dict] = []

    async def edit(self, **kw):
        self.edits.append(kw)
        if "content" in kw:
            self.content = kw["content"]

    async def add_reaction(self, e):
        self.added.append(e)

    async def remove_reaction(self, e, user):
        self.removed.append(e)


class _Devin:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, sid, content, **kw):
        self.sent.append((sid, content))
        return type("S", (), {"status": "running", "status_detail": None})()


class _Github:
    def __init__(self):
        self.approved = []

    async def approve_pr(self, ref):
        self.approved.append(ref)


class _Progress:
    def __init__(self):
        self.thinking_calls = 0

    async def thinking(self, b):
        self.thinking_calls += 1


class _Relay:
    def __init__(self):
        self.polls: list[str] = []
        self.polled_pr = None
        self.progress = _Progress()

    def request_poll(self, b):
        self.polls.append(b.session_id)

    async def _poll_pr(self, b, row, ref):
        self.polled_pr = ref


class _Settings:
    allowed_user_id_set = {1}
    create_as_user_id = None
    admin_user_id_set = frozenset()


class _Bot:
    def __init__(self, db, **kw):
        self.db = db
        self.devin = _Devin()
        self.github = _Github()
        self.relay = _Relay()
        self.settings = _Settings()
        self.user = object()
        self.__dict__.update(kw)


async def _bot(tmp_path, binding: Binding) -> _Bot:
    db = await Database.connect(str(tmp_path / "t.db"))
    await db.upsert_binding(binding)
    return _Bot(db)


def _binding(**kw) -> Binding:
    return Binding(
        session_id="s1", thread_id=10, channel_id=5, anchor_msg_id=100, **kw
    )


async def test_thumbs_up_on_anchor_sends_approval(tmp_path):
    bot = await _bot(tmp_path, _binding())
    msg = _Msg(100)  # the anchor
    await DevinMobileBot._handle_reaction(bot, _binding(), msg, "👍", 1)
    assert bot.devin.sent == [("s1", "Approved — please proceed.")]
    assert bot.relay.polls == ["s1"]
    assert bot.relay.progress.thinking_calls == 1


async def test_thumbs_up_on_pr_card_approves(tmp_path):
    bot = await _bot(tmp_path, _binding())
    await bot.db.upsert_pr(PrRow(
        session_id="s1", pr_url="https://github.com/o/r/pull/9",
        owner="o", repo="r", number=9, card_msg_id=200,
    ))
    msg = _Msg(200)
    await DevinMobileBot._handle_reaction(bot, _binding(), msg, "👍", 1)
    assert bot.github.approved  # approve_pr got the PullRef
    assert bot.github.approved[0].key == "o/r#9"
    assert any("approved" in s for s in msg.channel.sent)


async def test_repeat_on_anchor_polls(tmp_path):
    bot = await _bot(tmp_path, _binding())
    await DevinMobileBot._handle_reaction(bot, _binding(), _Msg(100), "🔁", 1)
    assert bot.relay.polls == ["s1"]


async def test_repeat_on_pr_card_refreshes(tmp_path):
    bot = await _bot(tmp_path, _binding())
    await bot.db.upsert_pr(PrRow(
        session_id="s1", pr_url="https://github.com/o/r/pull/9",
        owner="o", repo="r", number=9, card_msg_id=200,
    ))
    await DevinMobileBot._handle_reaction(bot, _binding(), _Msg(200), "🔁", 1)
    assert bot.relay.polled_pr is not None
    assert bot.relay.polled_pr.number == 9


async def test_repeat_resends_failed_message(tmp_path):
    bot = await _bot(tmp_path, _binding())
    msg = _Msg(300, content="fix the flake", reactions=[_Reaction("❌", me=True)])
    await DevinMobileBot._handle_reaction(bot, _binding(), msg, "🔁", 1)
    assert bot.devin.sent == [("s1", "fix the flake")]
    assert "✅" in msg.added and "❌" in msg.removed


async def test_repeat_on_normal_message_noops(tmp_path):
    bot = await _bot(tmp_path, _binding())
    msg = _Msg(300, content="just chatter")
    await DevinMobileBot._handle_reaction(bot, _binding(), msg, "🔁", 1)
    assert not bot.devin.sent and not bot.relay.polls


async def test_pause_parks_binding(tmp_path):
    binding = _binding()
    bot = await _bot(tmp_path, binding)
    msg = _Msg(100)
    await DevinMobileBot._handle_reaction(bot, binding, msg, "⏸️", 1)
    stored = await bot.db.get_binding("s1")
    assert stored is not None and not stored.active
    assert any("parked" in s for s in msg.channel.sent)


# ---- reply-quoting --------------------------------------------------------


class _Ref:
    def __init__(self, mid, resolved=None):
        self.message_id = mid
        self.resolved = resolved


async def _quote(bot, msg):
    return await DevinMobileBot._reply_quote(bot, msg)


async def test_quote_uses_referenced_content():
    src = _Msg(50, content="the first   attempt\nfailed badly")
    msg = _Msg(60, content="why?")
    msg.reference = _Ref(50, resolved=src)
    q = await _quote(object(), msg)
    assert q == "the first attempt failed badly"


async def test_quote_falls_back_to_embed():
    import discord

    e = discord.Embed(title="PR #9 — Add thing")
    e.url = "https://github.com/o/r/pull/9"
    src = _Msg(50, content="")
    src.embeds = [e]
    msg = _Msg(60)
    msg.reference = _Ref(50, resolved=src)
    q = await _quote(object(), msg)
    assert "PR #9 — Add thing" in q and "github.com" in q


async def test_quote_truncates_and_skips_empty():
    src = _Msg(50, content="x" * 500)
    msg = _Msg(60)
    msg.reference = _Ref(50, resolved=src)
    q = await _quote(object(), msg)
    assert q is not None and len(q) == 300
    src2 = _Msg(51, content="")
    msg2 = _Msg(61)
    msg2.reference = _Ref(51, resolved=src2)
    assert await _quote(object(), msg2) is None


# ---- choice buttons -------------------------------------------------------


class _Row:
    def __init__(self, children):
        self.children = children


class _Btn:
    def __init__(self, custom_id, label):
        self.custom_id = custom_id
        self.label = label


class _Response:
    def __init__(self):
        self.deferred = False
        self.messages: list[str] = []

    async def defer(self, **kw):
        self.deferred = True

    async def send_message(self, text, **kw):
        self.messages.append(text)


class _Followup:
    def __init__(self):
        self.messages: list[str] = []

    async def send(self, text, **kw):
        self.messages.append(text)


class _Interaction:
    def __init__(self, msg, custom_id, user_id=1):
        self.message = msg
        self.data = {"custom_id": custom_id}
        self.response = _Response()
        self.followup = _Followup()
        self.user = type("U", (), {"id": user_id})()


def _choice_msg(custom_id_prefix="dvm:choose:s1:"):
    msg = _Msg(400, content="Which approach?")
    msg.components = [
        _Row([
            _Btn(f"{custom_id_prefix}1", "1. rewrite the parser"),
            _Btn(f"{custom_id_prefix}2", "2. the caddy config"),
        ])
    ]
    return msg


async def _choose(bot, ix, session_id="s1", idx="2", binding=None):
    await DevinMobileBot._handle_choice(
        bot, ix, session_id, idx, binding or _binding()
    )


async def test_choice_button_sends_label(tmp_path):
    bot = await _bot(tmp_path, _binding())
    msg = _choice_msg()
    ix = _Interaction(msg, "dvm:choose:s1:2")
    await _choose(bot, ix)
    assert bot.devin.sent == [("s1", "2. the caddy config")]
    # buttons came off and the transcript records the pick
    assert msg.edits and msg.edits[0].get("view") is None
    assert "(answered: 2. the caddy config)" in msg.content
    assert bot.relay.progress.thinking_calls == 1
    assert bot.relay.polls == ["s1"]


async def test_choice_falls_back_to_index(tmp_path):
    bot = await _bot(tmp_path, _binding())
    msg = _Msg(400, content="Which?")  # no components to read a label from
    ix = _Interaction(msg, "dvm:choose:s1:2")
    await _choose(bot, ix)
    assert bot.devin.sent == [("s1", "2")]


async def test_choice_send_failure_keeps_buttons(tmp_path):
    bot = await _bot(tmp_path, _binding())

    class _FailDevin(_Devin):
        async def send_message(self, *a, **kw):
            raise RuntimeError("boom")

    bot.devin = _FailDevin()
    msg = _choice_msg()
    ix = _Interaction(msg, "dvm:choose:s1:1")
    await _choose(bot, ix, idx="1")
    assert any("Couldn't send" in m for m in ix.followup.messages)
    assert not msg.edits  # view untouched — retry stays possible

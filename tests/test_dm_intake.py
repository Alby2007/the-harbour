"""DM intake — `devin:` text, attachments, and voice notes spawn sessions;
stray chatter stays inert."""

import devinmobile.bot.main as main_mod
from devinmobile.bot.main import DevinMobileBot
from devinmobile.models import Session


class _Settings:
    allowed_user_id_set = {1}
    openai_api_key = ""


class _Att:
    def __init__(self, url, content_type="image/png", waveform=None):
        self.url = url
        self.content_type = content_type
        if waveform is not None:
            self.waveform = waveform


class _Msg:
    """Fake discord.Message — guild=None makes it a DM."""

    def __init__(self, content="", attachments=None, uid=1):
        self.content = content
        self.attachments = attachments or []
        self.author = type("U", (), {"id": uid, "bot": False})()
        self.guild = None
        self.channel = type("C", (), {"id": 42})()
        self.replies: list[str] = []
        self.reactions: list[str] = []

    async def reply(self, text, **kw):
        self.replies.append(text)
        return None

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


class _Thread:
    id = 4242
    jump_url = "https://discord.com/channels/555/4242"

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text, **kw):
        self.sent.append(text)


def _bot(monkeypatch, spawned, **settings_kw):
    """DevinMobileBot-shaped stub with spawn_session intercepted."""
    settings = type("S", (_Settings,), settings_kw)()
    bot = DevinMobileBot.__new__(DevinMobileBot)
    bot.settings = settings
    bot.github = None
    spawns: list[dict] = []

    async def fake_spawn(b, **kw):
        spawns.append(kw)
        t = _Thread()
        spawned.append(t)
        return Session(session_id="dm1", url="u", status="running"), t

    monkeypatch.setattr(main_mod, "spawn_session", fake_spawn)
    return bot, spawns


async def test_dm_photo_caption_spawns(monkeypatch):
    spawned: list = []
    bot, spawns = _bot(monkeypatch, spawned)
    msg = _Msg("what's wrong here", [_Att("https://cdn/shot.png")])
    await bot._dm_intake(msg)
    assert spawns[0]["prompt"] == "what's wrong here"
    assert spawns[0]["attachment_urls"] == ["https://cdn/shot.png"]
    assert "channels/555/4242" in msg.replies[0]
    assert spawned[0].sent == ["Spawned via DM."]


async def test_dm_photo_only_default_prompt(monkeypatch):
    bot, spawns = _bot(monkeypatch, [])
    msg = _Msg("", [_Att("https://cdn/shot.png")])
    await bot._dm_intake(msg)
    assert "no instructions" in spawns[0]["prompt"]
    assert spawns[0]["attachment_urls"] == ["https://cdn/shot.png"]


async def test_dm_bare_text_hints(monkeypatch):
    bot, spawns = _bot(monkeypatch, [])
    msg = _Msg("hey what's up")
    await bot._dm_intake(msg)
    assert not spawns
    assert "devin: <task>" in msg.replies[0]


async def test_dm_devin_prefix_spawns(monkeypatch):
    bot, spawns = _bot(monkeypatch, [])
    msg = _Msg("Devin: fix the login flake")
    await bot._dm_intake(msg)
    assert spawns[0]["prompt"] == "fix the login flake"
    # devin: alone — a prefix with nothing after it is a hint, not a spawn
    msg2 = _Msg("devin:")
    await bot._dm_intake(msg2)
    assert len(spawns) == 1 and "devin: <task>" in msg2.replies[0]


async def test_dm_voice_note_transcribes(monkeypatch):
    async def fake_transcribe(url, key):
        return "run the migration and report back"

    import devinmobile.transcribe
    monkeypatch.setattr(devinmobile.transcribe, "transcribe", fake_transcribe)
    spawned: list = []
    bot, spawns = _bot(monkeypatch, spawned, openai_api_key="sk")
    msg = _Msg("", [_Att("https://cdn/voice.ogg", "audio/ogg")])
    await bot._dm_intake(msg)
    assert spawns[0]["prompt"] == "run the migration and report back"
    # the consumed voice note doesn't ride as an attachment URL
    assert spawns[0]["attachment_urls"] is None
    assert "🎤" in msg.replies[0]


async def test_dm_voice_no_key(monkeypatch):
    bot, spawns = _bot(monkeypatch, [])
    msg = _Msg("", [_Att("https://cdn/voice.ogg", waveform="x")])
    await bot._dm_intake(msg)
    assert not spawns
    assert "OPENAI_API_KEY" in msg.replies[0]


async def test_dm_voice_with_caption_merges(monkeypatch):
    """Caption + voice note: the transcript IS content too — merged,
    never shipped as an inert audio URL."""
    async def fake_transcribe(url, key):
        return "check the diff in the photo"

    import devinmobile.transcribe
    monkeypatch.setattr(devinmobile.transcribe, "transcribe", fake_transcribe)
    spawned: list = []
    bot, spawns = _bot(monkeypatch, spawned, openai_api_key="sk")
    msg = _Msg(
        "and tell me if the approach is sane",
        [_Att("https://cdn/voice.ogg", "audio/ogg"),
         _Att("https://cdn/shot.png")],
    )
    await bot._dm_intake(msg)
    assert spawns[0]["prompt"] == (
        "check the diff in the photo\n\nand tell me if the approach is sane"
    )
    # the consumed voice note is gone; the photo still rides
    assert spawns[0]["attachment_urls"] == ["https://cdn/shot.png"]
    assert "🎤" in msg.replies[0]


async def test_dm_caption_voice_no_key(monkeypatch):
    """Caption + voice, no OPENAI_API_KEY: the caption still spawns, the
    audio URL is dropped, and the user is told why."""
    bot, spawns = _bot(monkeypatch, [])
    msg = _Msg(
        "look at this",
        [_Att("https://cdn/voice.ogg", "audio/ogg"),
         _Att("https://cdn/shot.png")],
    )
    await bot._dm_intake(msg)
    assert spawns[0]["prompt"] == "look at this"
    assert spawns[0]["attachment_urls"] == ["https://cdn/shot.png"]
    assert any("skipping the voice note" in r for r in msg.replies)


async def test_dm_spawn_error_replies(monkeypatch):
    """Spawn failures surface to the DM — a phone user must never get
    silence."""
    from devinmobile.spawn import SpawnError

    async def boom(b, **kw):
        raise SpawnError("HUB_CHANNEL_ID is not configured.")

    import devinmobile.bot.main as m
    bot, _ = _bot(monkeypatch, [])
    # override AFTER _bot installs its success stub
    monkeypatch.setattr(m, "spawn_session", boom)
    msg = _Msg("devin: fix it")
    await bot._dm_intake(msg)
    assert "HUB_CHANNEL_ID" in msg.replies[0]

    async def crash(b, **kw):
        raise RuntimeError("discord hiccup")

    monkeypatch.setattr(m, "spawn_session", crash)
    msg2 = _Msg("devin: fix it")
    await bot._dm_intake(msg2)
    assert "Spawn failed" in msg2.replies[0]


async def test_dm_stranger_silent(monkeypatch):
    bot, spawns = _bot(monkeypatch, [])
    msg = _Msg("devin: pwn", uid=999)
    await bot._dm_intake(msg)
    assert not spawns and not msg.replies

"""Voice-note transcription via OpenAI's audio API — a Discord voice
message's CDN URL is fetched, then POSTed as multipart to Whisper.
Feature is off unless OPENAI_API_KEY is set; plain httpx, no SDK.
"""

from __future__ import annotations

import httpx

API = "https://api.openai.com/v1/audio/transcriptions"
MODEL = "whisper-1"


async def transcribe(attachment_url: str, api_key: str, *, timeout: float = 60.0) -> str:
    """Download a Discord CDN attachment and transcribe it. Returns the
    transcript text ("" if nothing recognizable)."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        audio = await client.get(attachment_url)
        audio.raise_for_status()
        # keep the extension hint — whisper picks a decoder from it
        name = attachment_url.split("?")[0].rsplit("/", 1)[-1] or "voice.ogg"
        r = await client.post(
            API,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (name, audio.content)},
            data={"model": MODEL},
        )
        r.raise_for_status()
        return str(r.json().get("text") or "").strip()

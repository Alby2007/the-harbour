"""Probe: can the ACP bridge attach to an existing session and stream
session/update notifications?

The bridge's create flow already shows updates streaming during the FIRST
turn on the originating socket. Open questions this answers:

1. Does ``session/load`` accept a session created through the v3 API?
2. Do ``session/update`` notifications keep arriving on the loaded socket
   when the session is steered out-of-band (via v3 messages)?

Run from the repo root:  .venv/bin/python scripts/probe_load.py
"""

import asyncio
import json
import sys
import time
import tomllib
from pathlib import Path

import aiohttp
import httpx

ROOT = Path(__file__).resolve().parent.parent


def _env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


async def main() -> None:
    env = _env()
    api_key, org_id = env["DEVIN_API_KEY"], env["DEVIN_ORG_ID"]
    cred = tomllib.loads(
        Path("~/.local/share/devin/credentials.toml").expanduser().read_text()
    )
    token = cred["windsurf_api_key"]
    api_url = cred["devin_api_url"].rstrip("/")
    ws_url = api_url.replace("https://", "wss://") + f"/acp/live?token={token}"

    # 1. create a v3 session with enough work to stream updates
    async with httpx.AsyncClient(
        base_url="https://api.devin.ai/v3",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    ) as http:
        r = await http.post(
            f"/organizations/{org_id}/sessions",
            json={
                "prompt": (
                    "List every file in the repo root with ls -la, then open "
                    "README.md and summarize it in detail. Take your time."
                ),
                "repos": ["Alby2007/the-harbour"],
                "devin_mode": "lite",
                "max_acu_limit": 5,
            },
        )
        r.raise_for_status()
        sess = r.json()
        sid = sess["session_id"]
        print(f"[v3] created session {sid} status={sess.get('status')}")

    # 2. connect the bridge
    ids = iter(range(1, 10_000))

    async def rpc(ws, method, params, reads=200):
        rid = next(ids)
        await ws.send_str(
            json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        )
        while reads > 0:
            msg = await ws.receive()
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise RuntimeError(f"ws closed during {method}")
                continue
            data = json.loads(msg.data)
            if data.get("id") == rid:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result") or {}
            yield_note(data)
            reads -= 1
        raise RuntimeError(f"{method}: no response after {reads} reads")

    def yield_note(data: dict) -> None:
        if data.get("method") == "session/update":
            p = data.get("params", {})
            upd = p.get("update", {})
            kind = upd.get("sessionUpdate") or upd.get("type") or "?"
            detail = (
                upd.get("title")
                or upd.get("toolCallId")
                or (upd.get("content") or [{}])[0].get("text", "")[:80]
                if isinstance(upd.get("content"), list)
                else ""
            )
            print(f"  [update] {kind} :: {str(detail)[:100]}")

    async with aiohttp.ClientSession() as http_sess:
        ws = await http_sess.ws_connect(ws_url)
        await rpc(ws, "initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": "devinmobile-probe", "version": "0.1.0"},
        })
        print("[bridge] initialized")

        for cand in (f"devin-{sid}", sid):
            try:
                res = await rpc(
                    ws, "session/load",
                    {"sessionId": cand, "cwd": "/", "mcpServers": []},
                )
                print(f"[bridge] session/load OK with {cand!r} "
                      f"-> keys: {sorted(res.keys())}")
                loaded = cand
                break
            except Exception as e:  # noqa: BLE001
                print(f"[bridge] session/load {cand!r} failed: {e}")
                loaded = None
        if not loaded:
            print("RESULT: session/load does not work on v3 sessions")
            sys.exit(1)

        # 3. steer via v3 and watch updates stream on the loaded socket
        async with httpx.AsyncClient(
            base_url="https://api.devin.ai/v3",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        ) as http:
            r = await http.post(
                f"/organizations/{org_id}/sessions/{sid}/messages",
                json={"message": "Now run `pwd && git log --oneline -5` and report the output."},
            )
            print(f"[v3] steered -> {r.status_code}")

        deadline = time.time() + 120
        notes = 0
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(ws.receive(), 10)
            except TimeoutError:
                continue
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    print("[bridge] ws closed")
                    break
                continue
            data = json.loads(msg.data)
            if data.get("method") == "session/update":
                notes += 1
                yield_note(data)
        await ws.close()
        print(f"RESULT: {notes} session/update notifications observed on "
              f"loaded socket")


if __name__ == "__main__":
    asyncio.run(main())

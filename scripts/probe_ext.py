"""Probe: undocumented ACP surfaces — userShellCommand + session/set_mode.

Two suspected capabilities need live verification before UI is built:

1. ``cognition.ai/userShellCommand`` — a *prompt content block* (the CLI
   binary's validator says "multiple ... blocks are not allowed; send
   exactly one") that presumably runs a shell command in the session VM.
   The decisive question: does output land in the v3 transcript (relay
   picks it up free) or only in ephemeral session/update stream events?
2. ``session/set_mode`` — SetSessionModeRequest{sessionId, modeId} is in
   the binary's method table; session/new + session/load responses carry
   SessionModeState{currentModeId, availableModes[]}.

Strategy: spawn ONE cheap throwaway session ("Reply with exactly: OK"),
probe everything against it, then inspect its v3 transcript. Costs a
sliver of ACU. Errors ARE findings — "method not found" vs "invalid
params" vs "silent success" each say something different.

Run from the repo root:  .venv/bin/python scripts/probe_ext.py
"""

import asyncio
import json
import tomllib
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
MARKER = "probe-shell-marker-7f3a"


def _env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _summarize_update(update: dict) -> str:
    kind = update.get("sessionUpdate") or update.get("type") or "?"
    bits = [str(kind)]
    for key in ("title", "status", "kind", "toolCallId"):
        if update.get(key):
            bits.append(f"{key}={str(update[key])[:60]}")
    content = update.get("content")
    if isinstance(content, dict) and content.get("type"):
        bits.append(f"content.type={content['type']}")
    if update.get("currentModeId"):
        bits.append(f"mode={update['currentModeId']}")
    return " ".join(bits)


async def main() -> None:
    env = _env()
    org_id = env["DEVIN_ORG_ID"]
    api_key = env["DEVIN_API_KEY"]
    cred = tomllib.loads(
        Path("~/.local/share/devin/credentials.toml").expanduser().read_text()
    )
    token = cred["windsurf_api_key"]
    api_url = cred["devin_api_url"].rstrip("/")
    ws_url = api_url.replace("https://", "wss://") + f"/acp/live?token={token}"

    ids = iter(range(1, 10_000))
    updates: list[dict] = []  # every session/update notification seen

    async def rpc(ws, method, params, *, reads=3000, timeout=90):
        """One request; collects session/update notifications into
        `updates` while waiting (a prompt response arrives at turn end)."""
        rid = next(ids)
        await ws.send_str(
            json.dumps(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            )
        )
        try:
            for _ in range(reads):
                msg = await asyncio.wait_for(ws.receive(), timeout)
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        raise RuntimeError(f"ws closed during {method}")
                    continue
                data = json.loads(msg.data)
                if data.get("id") == rid:
                    if "error" in data:
                        raise RuntimeError(f"{method}: {data['error']}")
                    return data.get("result") or {}
                if data.get("method") == "session/update":
                    upd = (data.get("params") or {}).get("update") or {}
                    updates.append(upd)
                    print(f"    [update] {_summarize_update(upd)}")
        except TimeoutError:
            raise RuntimeError(f"{method}: no response in {timeout}s") from None
        raise RuntimeError(f"{method}: exhausted {reads} reads")

    async with aiohttp.ClientSession() as http_sess:
        ws = await http_sess.ws_connect(ws_url)
        init = await rpc(ws, "initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": "devinmobile-probe-ext", "version": "0.1.0"},
        })
        print(f"[initialize] {json.dumps(init)[:400]}")

        # -- 1. throwaway session -------------------------------------------
        new = await rpc(ws, "session/new", {"cwd": "/", "mcpServers": []})
        session_id = new.get("sessionId", "")
        print(f"\n[session/new] sessionId={session_id}")
        # 2. mode state dump — top-level AND per-configOption keys
        for key in ("modes", "availableModes", "currentModeId",
                    "sessionModeState", "modeState"):
            if key in new:
                print(f"[session/new].{key} = {json.dumps(new[key])[:800]}")
        mode_keys = [
            o.get("id") for o in new.get("configOptions", [])
            if "mode" in (o.get("id") or "").lower()
        ]
        print(f"[session/new] mode-ish configOption ids: {mode_keys}")

        print("\n[prompt] materializing with 'Reply with exactly: OK' ...")
        await rpc(ws, "session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text",
                        "text": "Reply with exactly: OK"}],
        }, timeout=300)
        bare_id = session_id.removeprefix("devin-")
        print(f"[prompt] turn ended; bare id {bare_id}")

        # -- session/load dumps SessionModeState too -------------------------
        try:
            loaded = await rpc(ws, "session/load", {
                "sessionId": session_id, "cwd": "/", "mcpServers": [],
            })
            print(f"\n[session/load] keys: {sorted(loaded.keys())}")
            for key in ("modes", "availableModes", "currentModeId",
                        "sessionModeState", "modeState"):
                if key in loaded:
                    print(f"[session/load].{key} = "
                          f"{json.dumps(loaded[key])[:800]}")
        except Exception as e:  # noqa: BLE001
            print(f"\n[session/load] -> {e}")
            loaded = {}

        # -- 3. session/set_mode attempts ------------------------------------
        avail: list[str] = []
        for src in (new, loaded):
            for key in ("availableModes",):
                for m in src.get(key) or []:
                    mid = m.get("id") if isinstance(m, dict) else str(m)
                    if mid and mid not in avail:
                        avail.append(mid)
            st = src.get("sessionModeState") or src.get("modes")
            if isinstance(st, dict):
                for m in st.get("availableModes") or []:
                    mid = m.get("id") if isinstance(m, dict) else str(m)
                    if mid and mid not in avail:
                        avail.append(mid)
        print(f"\n[set_mode] harvested modeIds: {avail or '(none)'}")
        for mode_id in [*avail, "plan", "act", "default", "ask"]:
            try:
                res = await rpc(ws, "session/set_mode", {
                    "sessionId": session_id, "modeId": mode_id,
                }, timeout=20)
                print(f"[set_mode {mode_id!r}] OK -> {json.dumps(res)[:300]}")
            except Exception as e:  # noqa: BLE001
                print(f"[set_mode {mode_id!r}] -> {e}")

        # -- 4. userShellCommand — every plausible wire shape ------------------
        # initialize advertised cognition.ai/userShellCommand: true, so the
        # surface exists; the block-in-prompt shape 422'd (validator only
        # accepts text/image/audio/resource blocks). Remaining guesses:
        # standalone method, top-level session/prompt param, block _meta.
        shapes: list[tuple[str, str, dict]] = [
            ("method cognition.ai/userShellCommand",
             "cognition.ai/userShellCommand",
             {"sessionId": session_id, "command": f"echo {MARKER}"}),
            ("method _cognition.ai/userShellCommand",
             "_cognition.ai/userShellCommand",
             {"sessionId": session_id, "command": f"echo {MARKER}"}),
            ("method cognition.ai/shellCommand",
             "cognition.ai/shellCommand",
             {"sessionId": session_id, "command": f"echo {MARKER}"}),
            ("prompt top-level userShellCommand param",
             "session/prompt",
             {"sessionId": session_id,
              "prompt": [{"type": "text", "text": "run the shell command"}],
              "userShellCommand": {"command": f"echo {MARKER}"}}),
            ("prompt top-level cognition.ai/userShellCommand param",
             "session/prompt",
             {"sessionId": session_id,
              "prompt": [{"type": "text", "text": "run the shell command"}],
              "cognition.ai/userShellCommand":
                  {"command": f"echo {MARKER}"}}),
            ("text block _meta",
             "session/prompt",
             {"sessionId": session_id,
              "prompt": [{"type": "text", "text": "ok",
                          "_meta": {"cognition.ai/userShellCommand":
                                    {"command": f"echo {MARKER}"}}}]}),
            ("request _meta",
             "session/prompt",
             {"sessionId": session_id,
              "prompt": [{"type": "text", "text": "ok"}],
              "_meta": {"cognition.ai/userShellCommand":
                        {"command": f"echo {MARKER}"}}}),
        ]
        for label, method, params in shapes:
            updates.clear()
            try:
                res = await rpc(ws, method, params, timeout=180)
                print(f"\n[userShellCommand {label}] OK -> "
                      f"{json.dumps(res)[:300]}")
                saw = [
                    _summarize_update(u) for u in updates
                    if MARKER in json.dumps(u)
                ]
                print(f"    updates containing marker: {saw or '(none)'}")
            except Exception as e:  # noqa: BLE001
                print(f"\n[userShellCommand {label}] -> {e}")
                if "closed" in str(e) or "no response" in str(e):
                    break  # socket/timeout — deeper problem, stop probing

        # last transport to try: a JSON-RPC *notification* (no id — ACP has
        # client→server notifications like session/cancel). If it executes,
        # the marker surfaces via v3/stream even without a response.
        updates.clear()
        await ws.send_str(json.dumps({
            "jsonrpc": "2.0",
            "method": "cognition.ai/userShellCommand",
            "params": {"sessionId": session_id,
                       "command": f"echo {MARKER}"},
        }))
        print("\n[userShellCommand notification] sent — draining 20s")
        try:
            deadline = asyncio.get_running_loop().time() + 20
            while True:
                msg = await asyncio.wait_for(
                    ws.receive(), max(0.1, deadline - asyncio.get_running_loop().time())
                )
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("method") == "session/update":
                        upd = (data.get("params") or {}).get("update") or {}
                        updates.append(upd)
                        print(f"    [update] {_summarize_update(upd)}")
        except TimeoutError:
            pass
        saw = [
            _summarize_update(u) for u in updates
            if MARKER in json.dumps(u)
        ]
        print(f"    updates containing marker: {saw or '(none)'}")

        # set_mode spelling variants — the real method may differ
        for method in ("session/setMode", "session/setSessionMode",
                       "session/set_config_option",):
            params = (
                {"sessionId": session_id, "modeId": "plan"}
                if "config_option" not in method
                else {"sessionId": session_id, "configId": "mode",
                      "value": "plan"}
            )
            try:
                res = await rpc(ws, method, params, timeout=15)
                print(f"[{method}] OK -> {json.dumps(res)[:300]}")
            except Exception as e:  # noqa: BLE001
                print(f"[{method}] -> {e}")

        # -- does the marker reach the v3 transcript? -------------------------
        await asyncio.sleep(5)  # let the last turn settle
        try:
            v3 = f"{api_url}/v3/organizations/{org_id}" \
                 f"/sessions/{bare_id}/messages"
            async with http_sess.get(
                v3, headers={"Authorization": f"Bearer {api_key}"},
            ) as resp:
                body = await resp.json()
            hits = [
                m for m in body.get("items", [])
                if MARKER in (m.get("message") or "")
            ]
            print(f"\n[v3 messages] status={resp.status} "
                  f"items={len(body.get('items', []))} "
                  f"marker hits={len(hits)}")
            for m in hits[:3]:
                print(f"    source={m.get('source')}: "
                      f"{(m.get('message') or '')[:200]}")
        except Exception as e:  # noqa: BLE001
            print(f"\n[v3 messages] -> {e}")

        # -- 5. opportunistic neighbors ----------------------------------------
        for method, params in (
            ("session/list", {}),
            ("cognition.ai/queuedMessages", {"sessionId": session_id}),
            ("_cognition.ai/queuedMessages", {"sessionId": session_id}),
            ("cognition.ai/sessionRename",
             {"sessionId": session_id, "title": "probe-ext"}),
            ("sessionHeartbeat", {"sessionId": session_id}),
            ("session/resume", {"sessionId": session_id}),
            ("session/close", {"sessionId": session_id}),
        ):
            try:
                res = await rpc(ws, method, params, timeout=15)
                print(f"[{method}] OK -> {json.dumps(res)[:300]}")
            except Exception as e:  # noqa: BLE001
                print(f"[{method}] -> {e}")

        await ws.close()


if __name__ == "__main__":
    asyncio.run(main())

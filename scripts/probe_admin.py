"""Probe: session-admin surfaces — v3 DELETE/PATCH, bridge session/delete
+ sessionArchiving.

Before /delete and /rename are built we need to know what actually exists:

1. ``PATCH /v3/organizations/{org}/sessions/{id}`` ``{"title": "…"}`` —
   is rename reachable via REST? (bridge ``sessionRename`` is advertised
   but probe-dead — see api-internals.md.)
2. Bridge ``cognition.ai/sessionArchiving`` — in the binary's method
   table; siblings all answered -32601, so likely the same.
3. Bridge ``session/delete`` — in the method table too, worth asking.
4. ``DELETE /v3/.../sessions/{id}`` — terminate_session's endpoint; the
   code has never verified it (kill swallows the result). Probed LAST
   since it kills the session; a bridge-side delete that works first
   would make this a 404-on-corpse, so we check liveness in between.

Strategy: create ONE throwaway session via v3 ("Reply with exactly:
OK"), probe the non-destructive ops first, delete last. Costs a sliver
of ACU. Errors ARE findings — -32601 vs silent-noop vs real status each
say something different.

Run from the repo root:  .venv/bin/python scripts/probe_admin.py
"""

import asyncio
import json
import tomllib
from pathlib import Path

import aiohttp

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
    org_id = env["DEVIN_ORG_ID"]
    api_key = env["DEVIN_API_KEY"]
    cred = tomllib.loads(
        Path("~/.local/share/devin/credentials.toml").expanduser().read_text()
    )
    token = cred["windsurf_api_key"]
    api_url = cred["devin_api_url"].rstrip("/")
    ws_url = api_url.replace("https://", "wss://") + f"/acp/live?token={token}"
    v3 = f"https://api.devin.ai/v3/organizations/{org_id}"
    auth = {"Authorization": f"Bearer {api_key}"}

    ids = iter(range(1, 10_000))

    async def rpc(ws, method, params, *, reads=2000, timeout=60):
        """One request; drains+prints session/update noise while waiting
        (a prompt response arrives at turn end)."""
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
                    kind = upd.get("sessionUpdate") or upd.get("type") or "?"
                    print(f"    [update] {kind}")
        except TimeoutError:
            raise RuntimeError(f"{method}: no response in {timeout}s") from None
        raise RuntimeError(f"{method}: exhausted {reads} reads")

    async with aiohttp.ClientSession() as http:
        # -- 0. throwaway session via v3 ------------------------------------
        async with http.post(
            f"{v3}/sessions", headers=auth,
            json={"prompt": "Reply with exactly: OK",
                  "title": "probe-admin throwaway",
                  "devin_mode": "lite"},
        ) as resp:
            body = await resp.text()
            print(f"[v3 POST /sessions] {resp.status} {body[:200]}")
            if resp.status >= 300:
                raise SystemExit("create failed — nothing to probe")
        session_id = json.loads(body).get("session_id") \
            or json.loads(body).get("sessionId") or ""
        print(f"[create] session_id={session_id}")
        if not session_id:
            raise SystemExit("no session id in create response")

        # -- 1. PATCH rename via REST ----------------------------------------
        # Try it immediately — the session may still be running its turn;
        # rename shouldn't depend on lifecycle state.
        for payload in ({"title": "probe-admin RENAMED"},):
            async with http.patch(
                f"{v3}/sessions/{session_id}", headers=auth, json=payload
            ) as resp:
                print(f"\n[v3 PATCH title] {resp.status} "
                      f"{(await resp.text())[:300]}")

        async with http.get(
            f"{v3}/sessions/{session_id}", headers=auth
        ) as resp:
            body = await resp.text()
            print(f"[v3 GET after PATCH] {resp.status} {body[:300]}")

        # -- 2. bridge probes --------------------------------------------------
        ws = await http.ws_connect(ws_url)
        init = await rpc(ws, "initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": "devinmobile-probe-admin",
                           "version": "0.1.0"},
        })
        print(f"\n[initialize] {json.dumps(init)[:300]}")

        # session/load accepts any org session (v3-created too)
        prefixed = session_id if session_id.startswith("devin-") \
            else f"devin-{session_id}"
        try:
            loaded = await rpc(ws, "session/load", {
                "sessionId": prefixed, "cwd": "/", "mcpServers": [],
            })
            print(f"[session/load] OK — keys: {sorted(loaded.keys())}")
        except Exception as e:  # noqa: BLE001
            print(f"[session/load] -> {e}")

        # archiving + delete — order: archiving first (non-destructive),
        # session/delete after (if it works the session is gone for v3)
        for method, params in (
            ("cognition.ai/sessionArchiving",
             {"sessionId": prefixed, "archived": True}),
            ("_cognition.ai/sessionArchiving",
             {"sessionId": prefixed, "archived": True}),
            ("session/delete", {"sessionId": prefixed}),
            ("session/delete", {"sessionId": session_id}),  # bare id spelling
        ):
            try:
                res = await rpc(ws, method, params, timeout=20)
                print(f"[{method} {params}] OK -> {json.dumps(res)[:300]}")
            except Exception as e:  # noqa: BLE001
                print(f"[{method} {params}] -> {e}")

        await ws.close()

        # -- 3. did bridge-side delete actually remove it? --------------------
        async with http.get(
            f"{v3}/sessions/{session_id}", headers=auth
        ) as resp:
            print(f"\n[v3 GET after bridge probes] {resp.status} "
                  f"{(await resp.text())[:300]}")

        # -- 4. v3 DELETE — the terminate endpoint, finally verified -----------
        async with http.delete(
            f"{v3}/sessions/{session_id}", headers=auth
        ) as resp:
            print(f"[v3 DELETE] {resp.status} {(await resp.text())[:300]}")

        async with http.get(
            f"{v3}/sessions/{session_id}", headers=auth
        ) as resp:
            print(f"[v3 GET after DELETE] {resp.status} "
                  f"{(await resp.text())[:200]}")


if __name__ == "__main__":
    asyncio.run(main())

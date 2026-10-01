"""Probe: what can the snapshot-setup surface actually do?

`_ensure_blueprints` already calls `list-blueprints` / `create-blueprint`
blindly. This answers, for warm-start support:

1. What fields does a blueprint carry (snapshot ids? setup state? status?)
2. Does `session/new` advertise a snapshot/blueprint config option we could
   set to pin a warm start?
3. Do other `_cognition.ai/snapshot-setup/*` methods exist (best-effort
   guesses — undocumented surface, errors are the answer).

Run from the repo root:  .venv/bin/python scripts/probe_snapshots.py
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
    cred = tomllib.loads(
        Path("~/.local/share/devin/credentials.toml").expanduser().read_text()
    )
    token = cred["windsurf_api_key"]
    api_url = cred["devin_api_url"].rstrip("/")
    ws_url = api_url.replace("https://", "wss://") + f"/acp/live?token={token}"

    ids = iter(range(1, 10_000))

    async def rpc(ws, method, params, reads=100):
        rid = next(ids)
        await ws.send_str(
            json.dumps(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            )
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
            reads -= 1
        raise RuntimeError(f"{method}: no response after {reads} reads")

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

        # 1. what does a blueprint actually contain?
        try:
            res = await rpc(
                ws,
                "_cognition.ai/snapshot-setup/list-blueprints",
                {"org_id": org_id},
            )
            bps = res.get("blueprints", [])
            print(f"[blueprints] {len(bps)} for org")
            for b in bps[:3]:
                print(json.dumps(b, indent=2)[:1500])
        except Exception as e:  # noqa: BLE001
            print(f"[blueprints] list failed: {e}")

        # 2. does session/new expose a snapshot/blueprint select?
        try:
            new = await rpc(
                ws, "session/new", {"cwd": "/", "mcpServers": []}
            )
            opt_ids = [
                o.get("id") for o in new.get("configOptions", []) if o.get("id")
            ]
            print(f"[session/new] configOption ids: {opt_ids}")
            for o in new.get("configOptions", []):
                oid = (o.get("id") or "").lower()
                if "snap" in oid or "blueprint" in oid or "image" in oid:
                    print(json.dumps(o, indent=2)[:1500])
        except Exception as e:  # noqa: BLE001
            print(f"[session/new] failed: {e}")

        # 3. guess sibling methods — errors ARE the finding
        for method, params in (
            ("_cognition.ai/snapshot-setup/get-blueprint",
             {"org_id": org_id, "repo_name": "Alby2007/the-harbour"}),
            ("_cognition.ai/snapshot-setup/update-blueprint",
             {"org_id": org_id, "repo_name": "x", "setup_script": "true"}),
            ("_cognition.ai/snapshot-setup/save-snapshot",
             {"org_id": org_id, "repo_name": "x"}),
        ):
            try:
                res = await rpc(ws, method, params)
                print(f"[{method}] OK -> {json.dumps(res)[:400]}")
            except Exception as e:  # noqa: BLE001
                print(f"[{method}] -> {e}")

        await ws.close()


if __name__ == "__main__":
    asyncio.run(main())

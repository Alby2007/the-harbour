#!/usr/bin/env python3
"""Phase-0 verification against the live Devin v3 API.

Reads DEVIN_API_KEY, DEVIN_ORG_ID, and optional DEVIN_BASE_URL from the env.

    python scripts/smoke.py create "describe the task" [--repo org/repo] [--mode lite]
                                              [--as-user user_abc] [--title t]
    python scripts/smoke.py status devin-abc123
    python scripts/smoke.py messages devin-abc123 [--after CURSOR]
    python scripts/smoke.py msg devin-abc123 "follow-up text"
    python scripts/smoke.py watch devin-abc123 [--interval 10]
"""

import argparse
import json
import os
import sys
import time

import httpx

BASE = os.environ.get("DEVIN_BASE_URL", "https://api.devin.ai/v3")
KEY = os.environ["DEVIN_API_KEY"]
ORG = os.environ["DEVIN_ORG_ID"]

CLIENT = httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {KEY}"}, timeout=30)

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests_passed": {"type": ["boolean", "null"]},
        "notes": {"type": "string"},
    },
    "required": ["summary"],
    "additionalProperties": True,
}


def call(method: str, path: str, **kw):
    resp = CLIENT.request(method, f"/organizations/{ORG}{path}", **kw)
    print(f"{method} {path} -> {resp.status_code}", file=sys.stderr)
    if resp.status_code >= 400:
        print(resp.text, file=sys.stderr)
        sys.exit(1)
    return resp.json()


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("prompt")
    c.add_argument("--repo", action="append")
    c.add_argument("--mode", choices=["normal", "fast", "lite", "ultra", "fusion"])
    c.add_argument("--as-user", dest="as_user")
    c.add_argument("--title")
    c.add_argument("--bypass-approval", action="store_true")

    s = sub.add_parser("status")
    s.add_argument("session")

    m = sub.add_parser("messages")
    m.add_argument("session")
    m.add_argument("--after")

    g = sub.add_parser("msg")
    g.add_argument("session")
    g.add_argument("text")
    g.add_argument("--as-user", dest="as_user")

    w = sub.add_parser("watch")
    w.add_argument("session")
    w.add_argument("--interval", type=int, default=10)

    args = ap.parse_args()

    if args.cmd == "create":
        body = {
            "prompt": args.prompt,
            "tags": ["discord-mobile", "smoke"],
            "structured_output_schema": SCHEMA,
        }
        if args.repo:
            body["repos"] = args.repo
        if args.mode:
            body["devin_mode"] = args.mode
        if args.as_user:
            body["create_as_user_id"] = args.as_user
        if args.title:
            body["title"] = args.title
        if args.bypass_approval:
            body["bypass_approval"] = True
        print(json.dumps(call("POST", "/sessions", json=body), indent=2))

    elif args.cmd == "status":
        print(json.dumps(call("GET", f"/sessions/{args.session}"), indent=2))

    elif args.cmd == "messages":
        params = {"first": 200}
        if args.after:
            params["after"] = args.after
        page = call("GET", f"/sessions/{args.session}/messages", params=params)
        print(json.dumps(page, indent=2))

    elif args.cmd == "msg":
        body = {"message": args.text}
        if args.as_user:
            body["message_as_user_id"] = args.as_user
        print(json.dumps(call("POST", f"/sessions/{args.session}/messages", json=body), indent=2))

    elif args.cmd == "watch":
        cursor = None
        while True:
            sess = call("GET", f"/sessions/{args.session}")
            detail = sess.get("status_detail")
            print(f"[{sess['status']}{'/' + detail if detail else ''}] "
                  f"acus={sess.get('acus_consumed')} prs={len(sess.get('pull_requests') or [])}")
            page = call(
                "GET", f"/sessions/{args.session}/messages",
                params={"first": 200, **({"after": cursor} if cursor else {})},
            )
            for item in page["items"]:
                print(f"  <{item['source']}> {item['message'][:200]}")
            if page.get("end_cursor"):
                cursor = page["end_cursor"]
            if sess["status"] in ("exit", "error", "suspended"):
                print("terminal status reached; structured_output:")
                print(json.dumps(sess.get("structured_output"), indent=2))
                break
            time.sleep(args.interval)


if __name__ == "__main__":
    main()

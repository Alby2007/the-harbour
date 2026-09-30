# Architecture

```
Discord (phone)                    bot host                      Devin
─────────────                      ────────                      ─────
/devin ──────┐                                             
             ├─ no model ──> v3 REST POST /sessions ─────────────────> session
             │                (service key, cog_)                    (v3-visible
             ├─ model:X ───> ACP bridge WS /acp/live                  immediately)
             │                (user key, credentials.toml)
             │                session/new -> set_config_option
             │                (devin_version, repos, platform)
             │                -> session/prompt ─────────────────────> session
             │                                                     materializes
             │                                                     into v3
             v
      thread + anchor embed + sqlite binding
             │
             └──────────────  relay loop (15s) ──────────────────────> v3 GET
                          messages cursor + status diff                    |
             │                                                             v
             └──────────────  thread msg -> v3 POST /messages ───────> steer
```

Two APIs, one session:

- **v3 REST** (`api.devin.ai/v3`, Bearer `cog_` service key) — create, poll
  status + messages, send follow-ups, PR links, structured output. Public and
  documented.
- **ACP bridge** (`wss://api.devin.ai/acp/live?token=`, the CLI's user
  credential) — used ONLY to *create* a session when `model:` is requested,
  because v3 has no model field. The moment the bridge session's first
  prompt lands it materializes into the org session store and everything —
  status, messages, steering — proceeds over v3 like any other session.
  Service-key perms (`ViewOrgSessions`/`ManageOrgSessions`) cover it.

Why not the bridge for everything? v3 gives us `tags`, `title`,
`max_acu_limit`, `structured_output_schema`, `attachment_urls`, and
`create_as_user_id` — none of which the bridge exposes. Bridge-created
sessions forgo those; they get attribution for free instead (created as the
credential's owner).

## Components

| Module | Role |
| --- | --- |
| `config.py` | pydantic-settings; all config via env |
| `devin_client.py` | async v3 client — retry/backoff on 429+5xx, typed `Session`/`MessagePage` |
| `acp_bridge.py` | WS JSON-RPC client: credentials.toml → `session/new` → `set_config_option` → `session/prompt`; fuzzy model resolution against live `configOptions` |
| `db.py` | `bindings` table: `session_id ↔ thread_id ↔ anchor_msg_id`, `msg_cursor`, `seen_event_ids` (dedupe), `active` flag, `model` label |
| `relay.py` | one poll loop: drain new `source=="devin"` messages into the thread, then diff `status`/`status_detail` → notifications. `active=0` parks dead sessions; a message reactivates |
| `embeds.py` | status + completion embeds (structured_output → summary/files/tests) |
| `views.py` | stateless buttons (`dvm:{action}:{session_id}` custom_ids survive restarts; `_INFLIGHT` dedupes the double-dispatch with `on_interaction`) |
| `bot/commands.py` | `/devin` `/sessions` `/devin-status`, model autocomplete, `_wait_for_v3` materialization poll |
| `bot/main.py` | client wiring, allowlist gate, thread→session steering (`on_message`), component dispatch |

## State model

A binding is `active` while the session might produce new messages; `exit`,
`error`, `suspended` park it (a user message in the thread reactivates —
send auto-resumes anyway, so old threads stay steerable forever).
`msg_cursor` + `seen_event_ids` (JSON list, capped 500) make restarts
idempotent: the poller refetches history but reposts nothing.

The v3 messages endpoint's `after` cursor only accepts the server's opaque
`end_cursor` (verified empirically — event ids are rejected), and
`end_cursor` is null on the tail page, so each poll refetches the full
history and dedupes locally. Fine at this scale.

## Failure handling

- v3: exponential backoff on 429/5xx (4 tries, ≤30s).
- Bridge: `BridgeError` on bad creds (403 → "re-run `devin auth login`"),
  RPC errors, timeouts (`BRIDGE_TIMEOUT`). `session/prompt` doesn't block on
  the turn — `_rpc` returns on the first `session/update` for the session.
- A failed create never leaves a registered binding.
- Per-binding poll exceptions are logged and isolated.

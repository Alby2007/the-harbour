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
                          messages cursor + status diff +                    |
                          pull_requests                                      v
             │            PR card -> thread; buttons ────────> GitHub API
             │            (merge/approve/close, CI rollup)    (App install
             │                                               token via JWT)
             └──────────────  thread msg -> v3 POST /messages ───────> steer

GitHub webhooks (optional) ──> aiohttp :PORT/github ──> prs lookup ──> thread
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

A third API exists for the PR surface: the **GitHub REST API**, authed as a
GitHub App installation (RS256 JWT → installation token, cached). Devin
opens PRs under its own identity — our App only needs to read PR/check
state and perform merge/approve/close on the org's repos.

## Components

| Module | Role |
| --- | --- |
| `config.py` | pydantic-settings; all config via env |
| `devin_client.py` | async v3 client — retry/backoff on 429+5xx, typed `Session`/`MessagePage` |
| `acp_bridge.py` | WS JSON-RPC client: credentials.toml → `session/new` → `set_config_option` → `session/prompt`; fuzzy model resolution against live `configOptions` |
| `db.py` | `bindings` table: `session_id ↔ thread_id ↔ anchor_msg_id`, `msg_cursor`, `seen_event_ids` (dedupe), `active` flag, `model` label. `prs` table: one row per (session, PR) — card msg id, last notified state, CI rollup |
| `relay.py` | one poll loop: drain new `source=="devin"` messages into the thread, then diff `status`/`status_detail` → notifications; syncs `session.pull_requests` → PR cards and polls state/CI for transitions. `active=0` parks dead sessions; a message reactivates |
| `github_client.py` | GitHub App client: PEM → RS256 JWT → installation token (cached ~55min); `get_pr`, `check-runs` rollup, `merge`, `approve`, `close`, `get_issue`. URL/issue-ref parsers and the checks-state reducer live here too |
| `webhook_server.py` | optional aiohttp receiver (`POST /github`, HMAC-SHA256 verified) — `pull_request`/`check_run`/`check_suite` events routed to the owning thread via the `prs` table. Polling covers the same transitions; this only lowers latency |
| `embeds.py` | status + completion embeds (structured_output → summary/files/tests) + `pr_embed` cards |
| `views.py` | stateless buttons (`dvm:{action}:{session_id}[:{extra}]` custom_ids survive restarts; `_INFLIGHT` dedupes the double-dispatch with `on_interaction`). `PRView` carries the PR number in `extra`; merges get a `MergeConfirmView` ephemeral step |
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

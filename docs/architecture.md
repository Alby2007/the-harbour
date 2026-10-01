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
             └──────────────  relay loop (5s) ───────────────────────> v3 GET
                          messages cursor + status diff +                    |
                          pull_requests                                      v
             │            PR card -> thread; buttons ────────> GitHub API
             │            (merge/approve/close/automerge,     (App install
             │             CI rollup, Fix-CI, review loop)     token via JWT)
             └──────────────  thread msg -> v3 POST /messages ───────> steer

SessionStream: one persistent bridge WS ── session/load per binding ──>
  session/update notifications ──> "Working…" message edited in place

GitHub webhooks (optional) ──> aiohttp :PORT/github ──> prs lookup ──> thread
                          └─> issues:labeled(devin) ──> spawn_session

schedules table ──> Scheduler task (60s) ──> spawn_session ──> new thread
```

Two APIs, one session:

- **v3 REST** (`api.devin.ai/v3`, Bearer `cog_` service key) — create, poll
  status + messages, send follow-ups, PR links, structured output. Public and
  documented.
- **ACP bridge** (`wss://api.devin.ai/acp/live?token=`, the CLI's user
  credential) — creates sessions when `model:` is requested (v3 has no
  model field), and also hosts the optional **SessionStream**: one
  persistent socket that `session/load`s each active session and relays
  its `session/update` tool-call notifications into the thread's live
  progress message. The moment a bridge session's first prompt lands it
  materializes into the org session store — everything else proceeds over
  v3 like any other session.
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
| `db.py` | `bindings` table: `session_id ↔ thread_id ↔ anchor_msg_id`, `msg_cursor`, `seen_event_ids` (dedupe), `active` flag, `model` label, `repos`, plus lifecycle columns `continued_from` (respawn/`/continue` parent — caps chains at depth 1), `max_acu` (per-task cap from `/devin budget:`), `review_of` (`owner/repo#n` for review-label spawns — dedupe + Post-review marker). `prs` table: one row per (session, PR) — card msg id, last notified state, CI rollup |
| `relay.py` | one poll loop: drain new `source=="devin"` messages into the thread, then diff `status`/`status_detail` → notifications; syncs `session.pull_requests` → PR cards and polls state/CI for transitions (opt-in `auto_merge` fires on green); one-shot `channel.typing()` while mid-turn; ACU-cap alerts at 80%/100% (per-task `max_acu` wins over global; hitting 100% parks the binding); quiet-streak watchdog; `error` transitions auto-respawn a seeded continuation via `_maybe_respawn` (once — `continued_from` blocks chains); bot presence mirrors the active-binding count. `request_poll` coalesces immediate re-polls (per-binding lock guards against the loop racing it). `active` derives from live status each poll — a message reactivates parked sessions. `on_progress` routes `agent_message_chunk` → reply streaming (canonical message reconciles the preview) |
| `acp_bridge.py` `SessionStream` | one persistent bridge WS: `session/load`s each active session, demuxes responses into pending futures while `session/update` notifications stream to `relay.on_progress` |
| `progress.py` | `ProgressTracker` — renders tool-call titles into ONE per-turn message edited in place (~3s throttle, last 6 lines), deleted on turn end. Also owns streamed replies: `agent_message_chunk` text accumulates into a live-edited reply (~1.2s), thinking placeholders promote into it, tool calls seal a reply segment, and `reconcile()` replaces the preview when the canonical v3 message lands |
| `spawn.py` | `spawn_session()` — the shared create path (repo canonicalization, blueprint ensure, bridge/v3 routing, thread+anchor+binding, first poll). `/devin`, `/devin-all`, `/schedule`, `/continue`, auto-respawn, and both label triggers all ride it; `budget`/`continued_from`/`review_of` land on the binding here. Probe finding: `session/new` exposes no snapshot select — blueprints (`_cognition.ai/snapshot-setup/*`, `scripts/probe_snapshots.py`) are the warm-start surface and already auto-apply per repo |
| `scheduler.py` | 60s tick over the `schedules` table → `spawn_session` per due row; `next_run_at` slides from fire-time so downtime can't storm |
| `transcribe.py` | Whisper via httpx multipart — Discord voice attachments → text for `on_message` steering. Off unless `OPENAI_API_KEY` is set |
| `github_client.py` | GitHub App client: PEM → RS256 JWT → installation token (cached ~55min); `get_pr`, `check-runs` rollup + `get_failed_checks`, `merge`, `approve`, `close`, `get_issue`(+comments), `get_pr_files`, `get_review_feedback`, `get_commit`, `get_file`, `get_pr_diff` (`.diff` media type, 400KB cap — attaches to completion cards). URL/issue-ref parsers and the checks-state reducer live here too |
| `links.py` | URL enrichment for steering: `github.com` issue/PR/commit/blob links → structured content via the App (private repos reachable); everything else → httpx GET + trafilatura extraction (regex stripper fallback). Caps: 3 links, ~3k chars each, ~8k total; failures become "unreadable here" notes so Devin can try its own browser |
| `webhook_server.py` | optional aiohttp receiver (`POST /github`, HMAC-SHA256 verified) — `pull_request`/`check_run`/`check_suite` events routed to the owning thread via the `prs` table; `pull_request_review*` posts comments with a Send-to-Devin button; `issues:labeled(GITHUB_TRIGGER_LABEL)` spawns a session; `pull_request:labeled(GITHUB_REVIEW_LABEL)` spawns a review session (bot-authored PRs skipped, one per PR via `review_of`). Polling covers state transitions; review/label paths are webhook-only |
| `embeds.py` | status + completion embeds (structured_output → summary/files/tests + GitHub diffstat) + `pr_embed` cards |
| `views.py` | stateless buttons (`dvm:{action}:{session_id}[:{extra}]` custom_ids survive restarts; `_INFLIGHT` dedupes the double-dispatch with `on_interaction`). `PRView` carries `owner/repo#n` in `extra`; merges get a `MergeConfirmView` ephemeral step; `FixCIView`/`ReviewNotifyView` are the one-button steering views |
| `bot/commands.py` | `/devin` (`budget:`) `/devin-all` `/schedule` (`recipe:`) `/continue` `/schedules` `/unschedule` `/sessions` `/usage` `/kill` `/devin-status`, model+repo autocomplete |
| `bot/main.py` | client wiring, allowlist gate, thread→session steering (`on_message`: reply-quoting, link enrichment, voice-note transcription), `on_raw_reaction_add` → 👍/🔁/⏸️ commands on anchor/PR-card/failed messages, component dispatch |

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
- Stream: reader death clears `_ws`/`_attached` and fails pending futures;
  the next poll's `attach()` re-opens and re-loads every bound session.
- A failed create never leaves a registered binding.
- Per-binding poll exceptions are logged and isolated.

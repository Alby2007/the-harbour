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

/chain ──> spawn_session(phase 0, chain={...}) ──> per-phase threads
   exit ──> relay._advance_chain: gate check (proceed / single_pr /
   ACU cap) ──> spawn next phase  ·  Ask ──> Continue→ button
   action phase (arm_automerge) runs bot-side, no session
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
| `db.py` | `bindings` table: `session_id ↔ thread_id ↔ anchor_msg_id`, `msg_cursor`, `seen_event_ids` (dedupe), `active` flag, `model` label, `repos`, plus lifecycle columns `continued_from` (respawn/`/continue`/chain parent — `child_of` is the resume idempotency key), `max_acu` (per-task cap from `/devin budget:`), `review_of` (`owner/repo#n` for review-label spawns — dedupe + Post-review marker), `chain` (playbook JSON — playbook/step/pending/cap/spent/pr_key/orig/auto/halted/title; `chain_resumable` backs the startup sweep), `summary` (structured_output.summary captured at completion — powers digest). `prs` table: one row per (session, PR) — card msg id, last notified state, CI rollup. `repo_notes` table: standing per-repo guidance injected into every spawn's prompt — `UNIQUE(repo, note)` dedupes the completion harvest, reads match `LOWER(repo)` |
| `relay.py` | one poll loop: drain new `source=="devin"` messages into the thread, then diff `status`/`status_detail` → notifications; syncs `session.pull_requests` → PR cards and polls state/CI for transitions (opt-in `auto_merge` fires on green); one-shot `channel.typing()` while mid-turn; ACU-cap alerts at 80%/100% (per-task `max_acu` wins over global; hitting 100% parks the binding); quiet-streak watchdog; `error` transitions auto-respawn a seeded continuation via `_maybe_respawn` (once — `continued_from` blocks chains); bot presence mirrors the active-binding count. `request_poll` coalesces immediate re-polls (per-binding lock guards against the loop racing it). `active` derives from live status each poll — a message reactivates parked sessions — unless an armed `auto_merge` PR is still open, which keeps a dead session's binding polling until the merge fires. `on_progress` routes `agent_message_chunk` → reply streaming (canonical message reconciles the preview). Chain runner: a `complete` transition on a `chain`-carrying binding calls `_advance_chain` — `advance()` picks Advance (spawn the child seeded with structured_output) / Ask (set `pending`; the completion card's Continue→ button — a failed spawn parks on `pending` too, so the button doubles as retry) / Halt (mark `chain.halted` + terminal `step`, post the reason); `action` phases run bot-side inside the advance loop. An errored phase posts a chain-halted notice — `/continue` is the manual retry. `_resume_chains` at startup re-advances exited phases that never spawned a `child_of` continuation. Completion also harvests `structured_output.repo_notes` into the `repo_notes` table (`_harvest_repo_notes` — deduped, posts a 📝 note count) |
| `acp_bridge.py` `SessionStream` | one persistent bridge WS: `session/load`s each active session, demuxes responses into pending futures while `session/update` notifications stream to `relay.on_progress` |
| `progress.py` | `ProgressTracker` — renders tool calls into ONE per-turn message edited in place (~3s throttle, last 6 lines), deleted on turn end. Lines are keyed by `toolCallId` so `tool_call_update` statuses patch in place (✓/✗, plus a failure output tail); `kind:"execute"` renders as `$ cmd`. Also owns streamed replies: `agent_message_chunk` text accumulates into a live-edited reply (~1.2s), thinking placeholders promote into it, tool calls seal a reply segment, and `reconcile()` replaces the preview when the canonical v3 message lands |
| `spawn.py` | `spawn_session()` — the shared create path (repo canonicalization, blueprint ensure, bridge/v3 routing, thread+anchor+binding, first poll). `/devin`, `/devin-all`, `/schedule`, `/continue`, auto-respawn, chain phases, and both label triggers all ride it; `budget`/`continued_from`/`review_of`/`chain` land on the binding here. Also injects `repo_notes` into every prompt (post-canonicalization, ≤8/repo, ~2k block) plus the one-line `structured_output.repo_notes` nudge that keeps the harvest loop self-filling. Probe finding: `session/new` exposes no snapshot select — blueprints (`_cognition.ai/snapshot-setup/*`, `scripts/probe_snapshots.py`) are the warm-start surface and already auto-apply per repo |
| `scheduler.py` | 60s tick over the `schedules` table, dispatch on `kind`: `spawn` → `spawn_session`; `digest` → `build_digest` rollup to hub; `monitor` → `run_check` then spawn only on a red edge (cooldown + still-running dedup); `inbox` → `build_inbox_embed` triage card. `next_run_at` slides from fire-time so downtime can't storm |
| `digest.py` | `build_digest(bindings, since)` — pure renderer: sessions grouped Completed / Errored / Suspended / In flight, each row title + `summary[:120]` + `<#thread>` + ACU; footer totals. Feeds `/digest` and digest-kind schedules |
| `monitors.py` | `parse_watch` (`https://…` / `ci:o/r[@branch]`) + `run_check` → `Check(ok\|red\|unknown, detail)`. URL probes via httpx (transport failure IS red — a down endpoint is the signal); `ci:` rides `github.get_ref_checks` (API failure → `unknown`, never red) |
| `inbox.py` | `build_inbox_embed(db)` gathers + `build_inbox` renders the prospective card: waiting (waiting_for_* + question excerpt), errored-minus-continued (`continued_parents`), `chain.pending`, red monitors, open PRs (+CI/armed); inbox-zero adds `idle_repos` suggestions |
| `transcribe.py` | Whisper via httpx multipart — Discord voice attachments → text for `on_message` steering. Off unless `OPENAI_API_KEY` is set |
| `github_client.py` | GitHub App client: PEM → RS256 JWT → installation token (cached ~55min); `get_pr`, `check-runs` rollup + `get_failed_checks`, `merge`, `approve`, `close`, `get_issue`(+comments), `get_pr_files`, `get_review_feedback`, `get_commit`, `get_file`, `get_pr_diff` (`.diff` media type, 400KB cap — attaches to completion cards). URL/issue-ref parsers and the checks-state reducer live here too |
| `links.py` | URL enrichment for steering: `github.com` issue/PR/commit/blob links → structured content via the App (private repos reachable); everything else → httpx GET + trafilatura extraction (regex stripper fallback). Caps: 3 links, ~3k chars each, ~8k total; failures become "unreadable here" notes so Devin can try its own browser |
| `webhook_server.py` | optional aiohttp receiver on `GITHUB_WEBHOOK_PORT` — routes register per credential: `POST /github` (HMAC-SHA256, `pull_request`/`check_run`/`check_suite` → owning thread via `prs`; `pull_request_review*` comments + Send-to-Devin; `issues:labeled`/`pull_request:labeled` spawn) and `POST /task` (`TASK_INTAKE_TOKEN` bearer — `{prompt, repo?, title?, budget?}` → `spawn_session` → `{session_id, thread_id, session_url, thread_url}`; non-Discord intake so the bot is the renderer, not the source). Polling covers state transitions; review/label paths are webhook-only |
| `embeds.py` | status + completion embeds (structured_output → summary/files/tests + GitHub diffstat) + `pr_embed` cards |
| `views.py` | stateless buttons (`dvm:{action}:{session_id}[:{extra}]` custom_ids survive restarts; `_INFLIGHT` dedupes the double-dispatch with `on_interaction`). `PRView` carries `owner/repo#n` in `extra`; merges get a `MergeConfirmView` ephemeral step; `FixCIView`/`ReviewNotifyView` are the one-button steering views; `ChoiceView` renders parsed question options — the option text lives on each button's label so `choose` clicks send the answer without any stored state; `CompletionView` decorates the completion card with any subset of Post-review + `chain_next` Continue→ |
| `choices.py` | `parse_choices` — pulls the closing enumerated list (numbered/lettered/bullets, 2–5 options) out of a Devin question for `ChoiceView`, with a Yes/No fallback for confirmation-shaped endings |
| `chains.py` | Playbook registry + gate eval. `Phase` (name/kind/prompt/gate/auto/action/review), `PLAYBOOKS` (`janitor`, `iterate`), `advance()` → `Advance`/`Ask`/`Halt` (pure — reads the finished session's `structured_output.proceed`, its tracked-PR count, and the chain's rolled ACU spend; an explicit `proceed=false` SKIPS the gated phase rather than killing the chain), `continued_chain()` carries chain state into `/continue`/respawn children, `render_prompt` formats `{orig}`/`{summary}`/`{files}`/`{notes}`/`{pr_key}`/`{pr_url}`/`{prev_url}`/`{repo}` templates |
| `bot/commands.py` | `/devin` (`budget:`) `/devin-all` `/schedule` (`recipe:`) `/continue` `/schedules` `/unschedule` `/sessions` `/usage` `/kill` `/devin-status`, `/chain` (`playbook:` — chain-wide `budget:` + `auto:`) `/chains`, `/note` `/notes` `/unnote` (thread-repo fallback for `/note`), `/digest` (`hours:`), `/inbox`, `/schedule` `kind:` (`spawn`|`digest`|`monitor`|`inbox` — monitor adds `watch:`/`expect:`/`cooldown:`), model+repo autocomplete |
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

# devinmobile — agent notes

Discord bot front-end for Devin Cloud sessions (v3 REST API,
`api.devin.ai/v3/organizations/{org_id}/...`). Thin client: the bot holds a
`cog_` service-user key server-side and maps `Discord thread ↔ Devin session`.

## Commands

- Setup: `cp .env.example .env`, fill values; `pip install -e .[dev]`
- Run: `python -m devinmobile.bot.main` (or `docker compose up --build`)
- Tests: `pytest` · Lint: `ruff check` · Types: `mypy src`
- API proof-of-concept without the bot: `python scripts/smoke.py --help`

## Architecture

- `devin_client.py` — async httpx wrapper. `list_messages` is cursor-paginated;
  `send_message` auto-resumes suspended sessions and accepts `attachment_urls`
  + `message_as_user_id`. Needs `ManageOrgSessions` permission (not just
  `UseDevinSessions`).
- `relay.py` — one poll loop over `active` bindings: drain new `source ==
  "devin"` messages into the thread (API-POSTed messages come back as
  `source == "user"` and are filtered so steering doesn't echo), then diff
  `status`/`status_detail` for notifications. `classify_transition` is pure —
  only fires on *changes* so waiting states don't re-ping.
- `db.py` — aiosqlite `bindings` table: session_id, thread_id, anchor_msg_id,
  msg_cursor, seen_event_ids (JSON, dedupe cap 500), active flag. `active=0`
  on exit/error/suspended; typing in the thread flips it back to 1.
- `bot/commands.py` — `/devin` (create session → create public thread in
  HUB_CHANNEL_ID → anchor embed + view → insert binding), `/sessions`,
  `/devin-status`. `/devin model:X` switches creation to the ACP bridge.
  `/chain playbook:…` spawns phase 0 with `chain={playbook, step:0, cap,
  spent:0, orig, auto}`; `/chains` lists chain bindings ephemerally.
- `acp_bridge.py` — model selection. v3's create schema has no `model` field
  (extra fields are silently ignored, not 422'd — verified), so `/devin
  model:X` creates the session over the internal ACP bridge the CLI/Desktop
  use: `wss://{devin_api_url}/acp/live?token=<windsurf_api_key>` from
  `~/.local/share/devin/credentials.toml` (written by `devin auth login` —
  one-time browser OAuth; the key is long-lived). Wire flow: `initialize` →
  `session/new` (returns `configOptions` — `devin_version` IS the model
  select, plus `repos`/`platform`/`persona_slug`, all server-advertised) →
  `session/set_config_option` → `session/prompt` (returns at turn end; we
  detach on the first `session/update` instead). A bridge session is a draft
  until its first prompt lands — THEN it materializes into v3 (confirmed:
  service key can get/list/poll/steer it, and `user_id` is the real user, no
  impersonation needed). Drafts never reach v3, so the prompt is mandatory.
  Tradeoff vs v3 create: no `tags`, `structured_output_schema`, or
  `max_acu_limit` on bridge sessions. `resolve_option` fuzzy-matches a free
  string to the advertised option values (MODEL_ALIASES for known names).
- `views.py` — stateless `dvm:{action}:{session_id}[:{extra}]` custom_ids;
  `_INFLIGHT` dedupes the double-dispatch between a live View callback and
  `Client.on_interaction` (Stockbot chart_view.py pattern). Buttons: Open
  (link), Refresh, Approve (sends "Approved — please proceed." via messages
  API), SSH (ephemeral `ssh <id>@ssh.devin.ai` string — Cognition's gateway,
  no tunneling needed). PR cards add `pr_merge`/`pr_merge_go`/`pr_approve`/
  `pr_close` — `extra` carries the PR number. `ChoiceView` (action `choose`)
  renders `choices.py`-parsed question options; the option text lives on
  the button LABEL (state stays on the message — clicks after a restart
  still resolve), `extra` carries only the 1-based index as fallback.
- `choices.py` — `parse_choices` maps a relayed Devin question to button
  options: `?`-gated, takes the LAST same-style list run (numbered/lettered
  must be sequential from 1/A; 2–5 items ≤120 chars), else a Yes/No
  fallback when the message ends with a confirmation-shaped `?`. Returns
  None on anything ambiguous — a stray button row is the worst failure.
- `github_client.py` — GitHub App client (optional). RS256 JWT from the app
  PEM → `POST /app/installations/{id}/access_tokens` → cached token (~55min
  TTL, 5min refresh margin). `parse_pr_url`/`parse_issue_ref`/`checks_state`
  are pure helpers. `checks_state` collapses check runs: any completed
  failure beats in-progress; skipped/neutral don't block success.
- `db.py` `prs` table — `(session_id, pr_url)` PK; `card_msg_id` (the posted
  card), `last_notified` (`"{state}|{checks}"` — notify once per distinct
  outcome), `binding_for_pr`/`get_pr_by_ref` are case-insensitive
  (owner/repo compare via `lower()` — URL casing vs webhook `login` casing
  can differ). Upsert is state-tolerant: NULLIF/CASE guards keep a
  state-only update from wiping owner/repo/number. PR button custom_ids
  carry `owner/repo#n` in `extra` — bare numbers collide across repos in a
  multi-repo session. `seen_event_ids` is an insertion-ordered *list* —
  the 500-cap evicts oldest-first (uuid event ids don't sort by time;
  sorted() eviction caused reposts). `bindings.last_msg` persists the last
  Devin message so `waiting_for_user`'s question detection works when the
  message and the status flip land in different poll ticks.
- `relay.py` PR path — `_sync_prs` runs inside `poll_binding`: discovers
  `session.pull_requests` (v3 field), posts one card per new PR
  (`_post_pr_card`), then `_poll_pr` diffs `(state, checks)` against
  `last_notified` and posts/mentions on transitions; `_refresh_pr_card`
  edits the card embed in place.
- `webhook_server.py` — optional aiohttp receiver on
  `GITHUB_WEBHOOK_PORT`/`/github`, only when `GITHUB_WEBHOOK_SECRET` is set.
  HMAC-SHA256 verify → `pull_request`/`check_run`/`check_suite` →
  `binding_for_pr` lookup → thread post. Untracked PRs are ignored. Polling
  converges on the same state, so this is a latency nicety, not a
  requirement.
- `/devin issue:`/branch:` — `issue` accepts URL / `owner/repo#n` / bare
  `#n` (resolved against `repo:`); title+body are fetched via the App and
  appended to the prompt, and the title defaults the thread name. `branch`
  adds a "checkout from origin/{branch}" prompt line — it's a prompt
  instruction, not a repo checkout on our side.

## Non-obvious constraints

- Bots cannot create threads in DM channels — sessions live in guild-channel
  threads. `/devin` still works from a DM; the thread lands in HUB_CHANNEL_ID.
- Security: every handler checks `ALLOWED_USER_IDS` (comma-separated
  snowflakes). Empty allowlist = bot refuses to start. `max_acu_limit` caps
  per-session spend.
- No outbound webhooks in v3 — polling only (default 15s + jitter).
- `GET /messages` returns chat text only, not tool-call progress. Streaming =
  Devin's messages + status transitions; that's the API ceiling.
- `GET /sessions` list requires a `qs` structured param (shape unverified) —
  `/sessions` reads the local DB instead of the list endpoint.
- Safe-mode approvals via message are assumed ("Approved — please proceed.");
  if the API ever requires in-app approval, the Open button covers it and
  `bypass_approval` exists on session create.
- Bridge gotchas: `session/prompt`'s JSON-RPC response arrives at turn END
  (minutes) — never wait for it directly; `_rpc(..., early_update_for=sid)`
  returns on the first update notification. `session_id` comes back
  `devin-`-prefixed; v3 uses the bare hex (strip it). `repos` config values
  are `owner/repo` strings (multi = comma-joined, observed single only).
  Two traps: non-option values are silently dropped (`currentValue` echoes
  `''`), so `resolve_option` canonicalizes case/substrings first; and a repo
  without a snapshot blueprint is NOT cloned (only `creationRepos` metadata
  updates — the VM falls back to the default repo). `AcpBridge.
  _ensure_blueprints` mirrors the CLI's ensure-blueprint step via
  `_cognition.ai/snapshot-setup/{list,create}-blueprints` before setting
  `repos`. `AcpBridge.catalog()` caches the configOptions for repo
  autocomplete.
  The bridge token is user-scoped OAuth — sessions made through it belong to
  that user and are NOT visible to the service key until first prompt.
- `SessionStream` (acp_bridge.py): one persistent bridge WS; `session/load`
  accepts `devin-{id}` for ANY org session (v3-created too — verified live)
  and `session/update` notifications stream tool_call titles while the
  session is steered out-of-band. `_rpc`'s read loop can't multiplex, so the
  stream has its own reader task + pending-future map. Socket death clears
  `_attached` — the next poll's `attach()` re-opens and re-loads.
  `progress.py` renders them into ONE edited "Working…" message per turn
  (deleted on turn end — don't post one message per tool call, it spams).
- `spawn.py::spawn_session` is THE create path — `/devin`, `/devin-all`,
  `/schedule` rows, chain phases (`relay._spawn_chain_phase`), and the
  `issues:labeled` webhook all call it. Anything spawn-wide
  (canonicalization, precedence, binding fields) belongs there —
  including the `repo_notes` prompt injection + `repo_notes` nudge line.
- Repo memory: `repo_notes` table (`UNIQUE(repo, note)` — harvest re-runs
  can't double-save). `/note` (thread→binding repo fallback), `/notes`,
  `/unnote`. `notes_for_repos` matches `LOWER(repo)` both sides, ≤8/repo
  newest-first, ~2k injected block. Completions harvest
  `structured_output.repo_notes` (schema declares it; prompts nudge it)
  via `relay._harvest_repo_notes` — `created_by="devin:<session>"`.
- `prs.auto_merge` is `INTEGER NULL`: None means "upsert doesn't carry the
  flag" so a state-only poll can't clobber an explicit toggle. Auto-merge
  checks run EVERY poll (not in the transition dedupe) because the flag can
  be flipped after CI is already green — and its own notice consumes the
  merged transition to avoid a double ping.
- `schedules.next_run_at` slides from fire-time, not due-time — catch-up
  storms after downtime are worse than missed runs. `schedules.kind` =
  `'spawn'` (default) or `'digest'` — digest rows post `build_digest`
  (digest.py) to the hub channel instead of spawning; `bindings.summary`
  is captured from `structured_output.summary` in `_notify`'s complete
  branch so digests stay a local read (`bindings_since` window via
  `COALESCE(last_activity_at, created_at)`).
- `webhook_server.py` routes register per credential: `/github` when the
  HMAC secret is set, `/task` when `TASK_INTAKE_TOKEN` is set; the server
  starts if either exists. `/task` is bearer auth (`hmac.compare_digest`)
  → `spawn_session` → returns `thread_url` (the Discord deep-link that
  makes Siri/Raycast intake useful). Same trust model as the webhook
  secret — no extra hardening beyond it.
- Attachments funnel through `spawn_session(attachment_urls=)`: `/devin`,
  `/devin-all`, `/continue`, `/chain` (phase 0 only) expose a native
  `attachment:` file option, `/task` takes `attachments` URLs, and DMs
  forward message attachments. v3 passes them as `attachment_urls`; the
  bridge path inlines them as `Attachments:` URL lines in the prompt (ACP
  resource_link blocks unverified — probe pending). Discord CDN URLs
  expire ~24h → NEVER on `/schedule` (a delayed spawn would deliver dead
  links).
- DM intake (`main._dm_intake`): `message.guild is None` → `devin:`
  prefix (any text), attachment (caption or default analyze prompt), or
  voice note (Whisper, `from_voice` flag). A caption + voice note merges
  `transcript\n\ncaption` — a `.ogg` URL is useless to Devin, so a
  consumed voice file NEVER rides as an attachment (thread path same).
  Bare non-prefixed text → one-line hint; non-allowlisted → silence.
  Prefix strips before the voice merge so a dictated "devin: …"
  transcript works.
- Webhook `_post` takes `view=`; fake test channels must accept `**kw`.
- Voice steering: `message.attachments` with `audio/*` (or `.waveform`)
  transcribe via Whisper when `OPENAI_API_KEY` is set; the transcript
  echoes as a quote so the user sees what Devin got.
- `bindings.continued_from` marks respawn/`/continue`/chain children — the
  respawn guard is "child doesn't respawn", capping respawns at depth 1;
  it's also `child_of`'s lookup key, which makes chain advance idempotent
  across a crash between deciding and spawning. `bindings.max_acu`
  overrides the global cap for `_check_acu` pings and parks the binding at
  100% (it enforces bridge sessions bot-side too, where v3's
  `max_acu_limit` can't reach — derived-active must be computed BEFORE
  `_check_acu` or the same tick un-parks it). `bindings.review_of`
  dedupes `devin-review` label spawns and puts a Post-review button on the
  completion card (COMMENT reviews only — never APPROVE).
- Playbook chains (`chains.py` + `bindings.chain` JSON): each phase is a
  `continued_from` child binding carrying
  `{playbook, step, pending, cap, spent, pr_key, orig, auto, halted,
  title}` — no chain table. `advance()` (pure) gates on
  next-phase-existence → `structured_output.proceed` (explicit `False`
  SKIPS the phase — the scan continues to the next one, so a clean
  iterate review still reaches automerge; missing continues) →
  `single_pr` (exactly one tracked PR; banks `pr_key` for downstream
  phases) → chain ACU cap (`spent` rolls up across phases — a dead
  phase's burn is rolled in via `continued_chain`). `relay
  ._advance_chain` runs after the completion notify: Advance spawns via
  `spawn_session` (repos/model/budget inherited; remaining-cap = `cap −
  spent`, floored at 1; `chain.title` keeps thread names from accreting
  ` · phase` suffixes), Ask sets `chain.pending` and the completion
  card's `Continue →` button. A failed spawn also parks on `pending` +
  posts a retry button — `chain_next` force-advances under the
  per-session relay lock (pending is re-read inside, so double-taps
  can't double-spawn; stale taps answer ephemerally). Halt sets
  `chain.halted` + terminal `step=len(phases)` and posts the reason; a
  halted chain ignores further completions and Continue buttons.
  `kind="action"` phases (arm_automerge) run bot-side inside the advance
  loop — no session — and arm the banked PR's `prs.auto_merge` then
  resurrect the producer binding (`active=1`) so `_poll_pr` can actually
  fire the merge; the same `has_armed_open_pr` check in `_poll` keeps
  that binding alive until it does. Startup calls `_resume_chains`
  (chain bindings with `status='exit'` and no halted/pending/terminal
  marker/child) so a crash mid-advance doesn't strand a chain. An errored
  phase can't auto-respawn past depth 1 — `_note_chain_halt` posts a
  notice and `/continue` is the manual retry (it carries the chain via
  `continued_chain`: pending/halted stripped, `spent` rolled).
  `/kill` on a phase parks the binding — parked sessions can't complete,
  so the chain dies with it. `/chain` refuses PR-gated playbooks when
  the GitHub App isn't configured (`_sync_prs` can't fill `prs`, so
  `single_pr` would halt on zero).
- `Relay._presence` writes bot status only when the active-binding count
  changes, skips until `is_ready()` (the first tick can precede the ws),
  and clears at zero — all failures are swallowed, it's cosmetic.
- Snapshot warm-start: probed (`scripts/probe_snapshots.py`) — `session/new`
  has no blueprint select and there are no update/save methods; blueprints
  auto-apply per repo so there's nothing left to build.
- Bridge extension probe (`scripts/probe_ext.py`, Oct 2026): the CLI binary's
  method table overstates the live surface. `session/set_mode`,
  `session/resume`, `session/close`, `queuedMessages`, `sessionRename`,
  `sessionHeartbeat` all → -32601; `userShellCommand` is advertised in
  `initialize` capabilities but rejected/ignored in every wire shape (no
  `/exec` possible — a user shell needs a normal steering turn);
  `session/list` does work. Full table in docs/api-internals.md.
- Test fixture leak: `Database.connect`'s aiosqlite worker thread is
  non-daemon — tests that skip `db.close()` leave pytest unable to exit
  (zombie processes accumulate). `tests/conftest.py` auto-closes every
  opened connection; keep new tests inside that convention.

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
- `views.py` — stateless `dvm:{action}:{session_id}` custom_ids; `_INFLIGHT`
  dedupes the double-dispatch between a live View callback and
  `Client.on_interaction` (Stockbot chart_view.py pattern). Buttons: Open
  (link), Refresh, Approve (sends "Approved — please proceed." via messages
  API), SSH (ephemeral `ssh <id>@ssh.devin.ai` string — Cognition's gateway,
  no tunneling needed).

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
  The bridge token is user-scoped OAuth — sessions made through it belong to
  that user and are NOT visible to the service key until first prompt.

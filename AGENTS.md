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
  `/devin-status`.
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

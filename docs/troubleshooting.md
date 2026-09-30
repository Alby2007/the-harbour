# Troubleshooting

## Bot won't start

- `DISCORD_TOKEN is not set` / `DEVIN_API_KEY / DEVIN_ORG_ID are not set` /
  `ALLOWED_USER_IDS is empty` — fix `.env`. The allowlist is mandatory on
  purpose.
- Login fails / `401 Unauthorized` — token is stale; regenerate in the dev
  portal.
- Bot connects but no slash commands — global sync takes up to an hour; set
  `COMMAND_GUILD_ID` to your server for instant sync, or wait.

## Bot invited but not in the member list

The portal's Install link defaults to a *user* install. Use a guild-install
URL (`integration_type=0` + `bot` scope — see [setup](setup.md#1-discord)).

## `/devin` says "HUB_CHANNEL_ID is not configured"

`HUB_CHANNEL_ID` empty, not a text channel, or the bot can't see it. Private
channels need the bot to have access (category permissions inherit).

## `model:` says "needs devin auth login"

No `credentials.toml` at `DEVIN_CREDENTIALS_PATH` on the bot host. Run
`devin auth login` there (or `--force-manual-token-flow` headless). If it
exists but the bridge says `rejected auth (403)`, the credential is stale —
log in again.

## `Bridge create failed: unknown model '...'`

The input didn't match the server's advertised `devin_version` catalog — the
error lists the valid names. Aliases like `swe2-max`/`opus`/`fusion` are
the safe spellings.

## "session ... not visible to the API yet"

The bridge created+prompted a session but v3 hasn't materialized it within
~20s. The session usually still exists in the web app — retry `/devin` or
watch `app.devin.ai`. Transient ordering, not a stuck state.

## Thread stops relaying / no pings

- Session reached `exit`/`error`/`suspended` → binding parks (`active=0`).
  Typing in the thread reactivates it.
- Bot restarted mid-run is fine — bindings and cursors are in SQLite.
- Check the bot log (`poll failed for <session>` lines) — a 401 means the
  `cog_` key rotated; a persistent 403 on a bridge session means it never
  materialized (no prompt landed).

## Double messages after restart

Shouldn't happen — `seen_event_ids` dedupes. If it does, the write of the
binding raced the poll; file it.

## Steering shows ❌

The `POST /messages` failed — session deleted/archived in the web app, or
the service key lost `ManageOrgSessions`. The exception is in the log.

## High ACU burn

`MAX_ACU_LIMIT` only guards v3-created sessions. Bridge (`model:`) sessions
have no cap; `swe2-max` and the priority/preview tiers are the expensive
ones — prefer `lite`/`swe2-medium` for routine phone tasks.

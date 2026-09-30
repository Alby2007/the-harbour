# Setup

Three pieces of credentials: a Discord bot, a Devin service-user key, and
(optionally, for model selection) a `devin` CLI login. Then one `.env` file.

## 1. Discord

1. <https://discord.com/developers/applications> → **New Application** →
   name it (e.g. "Devin Mobile").
2. **Bot** tab → **Reset Token** → copy → `DISCORD_TOKEN`.
3. Same tab → enable **Message Content Intent** (required — the bot reads
   thread messages to forward them as steering input).
4. Invite the bot to a server you control. Use the OAuth2 URL generator or
   this template (replace `CLIENT_ID` with the Application ID):

   ```
   https://discord.com/oauth2/authorize?client_id=CLIENT_ID&permissions=309237730368&integration_type=0&scope=bot%20applications.commands
   ```

   `integration_type=0` forces a *guild* install — without it the portal's
   "Install" link can install the app only for your user, and the bot never
   appears in the member list. The permission integer covers: View Channel,
   Send Messages, Send Messages in Threads, Create Public Threads, Add
   Reactions, Embed Links, Read Message History.
5. In that server, create a **private text channel** (e.g. `#devin`). Enable
   developer mode in Discord settings → right-click the channel → **Copy
   Channel ID** → `HUB_CHANNEL_ID`.
6. Your profile → **Copy User ID** → `ALLOWED_USER_IDS` (comma-separated for
   several users).

   Threads are required for the per-session UX, and Discord only allows them
   inside guild channels — never in DMs. `/devin` can be *invoked* in a DM,
   but the session thread is always created in `HUB_CHANNEL_ID`.

## 2. Devin service user (v3 API)

1. <https://app.devin.ai> → **Settings → Service Users** → create one with
   the **Admin** role. Admin grants the four permissions this bot uses:
   `UseDevinSessions` (create), `ViewOrgSessions` (poll),
   `ManageOrgSessions` (send follow-ups — required, the messages endpoint
   rejects without it), and `ImpersonateOrgSessions` (`create_as_user_id`
   attribution).
2. Copy the `cog_...` key → `DEVIN_API_KEY`.
3. Org ID is on the same page (`org-...`) → `DEVIN_ORG_ID`.
4. Optional attribution: your real user ID (`user-...`) →
   `CREATE_AS_USER_ID`. Find it via `GET /v3/organizations/{org}/members`
   or from a session you own. Sessions then appear under your account in
   the web app instead of under the service user.

## 3. Devin CLI login (model selection)

Only needed if you want `/devin model:...`. The v3 API exposes `devin_mode`
but not the real model picker, so model-selected sessions are created over
the internal ACP bridge that the CLI/Desktop use — and that bridge
authenticates with your *user* credentials.

```bash
devin auth login            # browser flow (auto-completes if you're signed in)
# headless host:
devin auth login --force-manual-token-flow   # prints a URL, you paste back a code
```

This writes `~/.local/share/devin/credentials.toml` (`windsurf_api_key` +
`devin_api_url`). The key is long-lived. Point `DEVIN_CREDENTIALS_PATH` at it
if it lives somewhere nonstandard.

Bridge-created sessions are owned by the logged-in user directly —
`CREATE_AS_USER_ID` is irrelevant for them. See
[api-internals.md](api-internals.md) for how the bridge works.

## 4. `.env`

```bash
cp .env.example .env
```

| Variable | Required | Notes |
| --- | --- | --- |
| `DISCORD_TOKEN` | yes | Bot token |
| `DEVIN_API_KEY` | yes | `cog_...` service-user key |
| `DEVIN_ORG_ID` | yes | `org-...` |
| `ALLOWED_USER_IDS` | yes | Comma-separated Discord snowflakes; empty = refuse everything |
| `HUB_CHANNEL_ID` | yes | Private guild text channel for session threads |
| `DEVIN_MODE` | no | Default v3 mode (`lite`, default). Ignored when `model:` is used |
| `MAX_ACU_LIMIT` | no | Per-session ACU cap, v3-created sessions only (default 25) |
| `CREATE_AS_USER_ID` | no | `user-...` for attribution on v3-created sessions |
| `COMMAND_GUILD_ID` | no | Guild ID → instant slash sync; global sync (~1h) happens anyway |
| `POLL_INTERVAL_SECONDS` | no | Message/status poll cadence (default 15) |
| `DB_PATH` | no | SQLite file (default `./devinmobile.db`) |
| `DEVIN_CREDENTIALS_PATH` | no | CLI credentials (default `~/.local/share/devin/credentials.toml`) |
| `DEVIN_API_URL_OVERRIDE` | no | Enterprise/staging API host |
| `BRIDGE_TIMEOUT` | no | Seconds for bridge calls (default 60) |

## 5. Run

```bash
python -m venv .venv && .venv/bin/pip install -e .[dev]
python -m devinmobile.bot.main
```

Expected log: `online as Devin#xxxx`, one poll loop running. Verify the API
path before the bot if you want — `python scripts/smoke.py create "ping"`
then `watch <id>` exercises create/messages/status end-to-end.

Smoke test from Discord:

```
/devin prompt:"print the first 10 fibonacci numbers" mode:lite
```

A thread should appear in your hub channel, the anchor embed lands, Devin's
reply streams in, and you get pinged when the turn ends.

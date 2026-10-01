# Setup

Credentials: a Discord bot, a Devin service-user key, and two optional
add-ons — a `devin` CLI login (model selection) and a GitHub App (PR
actions/CI/`issue:`). Then one `.env` file.

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

## 4. GitHub App (PR actions, CI, `issue:`)

Optional — without it everything works, PRs just stay read-only links. With
it you get PR cards with **Merge / Approve / Close** buttons, CI status in
the cards + pings on transitions, and `/devin issue:` intake.

1. <https://github.com/settings/apps> (or your org's settings) → **New
   GitHub App**:
   - Any name; homepage URL can be a placeholder; webhook can be left off
     for now (polling covers transitions).
   - **Repository permissions**: *Pull requests: Read & write*, *Contents:
     Read & write* (merge needs it), *Issues: Read-only*, *Checks:
     Read-only*.
   - **Subscribe to events**: `pull_request`, `pull_request_review`,
     `pull_request_review_comment`, `check_run`, `check_suite`, `issues`
     (only needed if you also want the webhook receiver below — the labels
     ride `pull_request`/`issues` `labeled` actions).
2. **Generate a private key** → save the `.pem` next to the bot →
   `GITHUB_APP_PRIVATE_KEY_PATH`.
3. App page → App ID → `GITHUB_APP_ID`.
4. **Install App** onto the account/org that owns your repos → the
   installation page URL ends in the installation id →
   `GITHUB_APP_INSTALLATION_ID`.
5. Optional instant transitions: set a **webhook** on the app pointing at
   `https://<your-host-or-tunnel>/github` with a shared secret →
   `GITHUB_WEBHOOK_SECRET` (+ `GITHUB_WEBHOOK_PORT`, default 8977). The bot
   verifies `X-Hub-Signature-256`. Tracked PRs get instant transitions;
   untracked ones are ignored except `devin`/`devin-review` labels, which
   spawn sessions.
   Behind NAT, a `cloudflared`/`ngrok` tunnel to the port works fine.

Devin's own GitHub connection is unaffected — sessions clone and open PRs
through Devin's identity either way; this App only acts on the results.

## 5. `.env`

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
| `POLL_INTERVAL_SECONDS` | no | Message/status poll cadence (default 5) |
| `DB_PATH` | no | SQLite file (default `./devinmobile.db`) |
| `DEVIN_CREDENTIALS_PATH` | no | CLI credentials (default `~/.local/share/devin/credentials.toml`) |
| `DEVIN_API_URL_OVERRIDE` | no | Enterprise/staging API host |
| `BRIDGE_TIMEOUT` | no | Seconds for bridge calls (default 60) |
| `GITHUB_APP_ID` | GitHub | App ID — enables the whole GitHub surface when all three are set |
| `GITHUB_APP_PRIVATE_KEY_PATH` | GitHub | Path to the app `.pem` |
| `GITHUB_APP_INSTALLATION_ID` | GitHub | From the install-page URL |
| `GITHUB_MERGE_METHOD` | no | `squash` (default) / `merge` / `rebase` |
| `GITHUB_WEBHOOK_SECRET` | no | Enables the `/github` webhook receiver |
| `GITHUB_WEBHOOK_PORT` | no | Webhook listen port (default 8977) |
| `GITHUB_TRIGGER_LABEL` | no | Issue label that spawns a session (default `devin`) |
| `GITHUB_REVIEW_LABEL` | no | PR label that spawns a review session (default `devin-review`) |
| `TASK_INTAKE_TOKEN` | no | Bearer token enabling `POST /task` (curl/Siri/Raycast → spawn session) on the webhook port; empty = off. Same exposure as the webhook secret — keep behind the same tunnel |
| `DEVIN_DEFAULT_MODEL` | no | Route every `/devin` through the bridge model unless `model:`/`mode:` is given |
| `ACP_PROGRESS` | no | Live "Working…" tool-call streaming via the bridge (default on; `0` disables) |
| `AUTO_RESPAWN` | no | Errored sessions respawn once as seeded continuations (default on; `0` disables) |
| `SILENCE_ALERT_MINUTES` | no | Quiet-streak before a "still working?" note (default 20, `0` disables) |
| `OPENAI_API_KEY` | no | Enables voice-note steering via Whisper transcription |

## 6. Run

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

# Deployment

The bot is a single long-lived process. It needs outbound HTTPS to
discord.com + api.devin.ai, and outbound WSS for the bridge. SQLite keeps
all state in one file — put it on a volume.

## Docker

```bash
docker compose up -d --build
```

`docker-compose.yml` runs the image's non-root user, sets
`DB_PATH=/data/devinmobile.db`, and mounts the `devinmobile-data` volume for
it. The Dockerfile builds on python:3.13-slim and installs the package.

### Model selection on a host

The bridge needs the CLI credential file. On the VM:

```bash
devin auth login --force-manual-token-flow   # prints a URL -> paste the code
# writes ~/.local/share/devin/credentials.toml
```

Then uncomment the read-only mount in `docker-compose.yml` (host path
→ `/home/app/.local/share/devin/credentials.toml` — match the container
user's home) and set `DEVIN_CREDENTIALS_PATH` to the container path.

Without the file the bot still runs fine — `/devin model:` replies with a
clear "not logged in" message and everything else works.

## Restarts

Safe at any time. On boot the bot reloads bindings from SQLite and resumes
polling `active` sessions; `seen_event_ids` prevents reposting. Threads of
suspended sessions stay steerable — a message reactivates them.

## Sizing / limits

- Poll interval 15s + jitter; each active session = 1 `list_messages` +
  1 `get_session` per tick. Scale the interval if you keep many sessions hot.
- Discord: messages chunked at ~1900 chars, max 8 chunks per Devin message;
  oversized transcripts degrade to a "see session" pointer.
- `MAX_ACU_LIMIT` bounds each v3-created session (default 25). Bridge
  sessions have no API-side cap — the lever there is your own judgment per
  task.

## Security

- `DISCORD_TOKEN`, `DEVIN_API_KEY`, `credentials.toml`'s
  `windsurf_api_key`, and the GitHub App `.pem` stay server-side; `.env`
  is gitignored. The App's installation token is minted in-memory and never
  persisted.
- `ALLOWED_USER_IDS` gates every command, every component click, and thread
  forwarding. An empty allowlist refuses to start — a Discord user with
  access could otherwise spend ACUs (or merge a PR).
- Keep the hub channel private: anyone who can *read* it sees session
  transcripts; anyone who can *write* to it can't steer (the allowlist still
  applies) but that's the threat model to keep in mind.
- The webhook receiver only processes PRs already in the `prs` table and
  rejects anything without a valid `X-Hub-Signature-256` — a forged POST
  can't reach threads, and an unguessed PR can't be looked up.

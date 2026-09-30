# devinmobile

A Discord bot that puts Devin Cloud sessions in your pocket. DM the bot (or
use a server channel) to launch sessions, watch them work, and steer them —
all from your phone.

```
/devin prompt:"fix the flaky auth test" repo:Alby2007/api model:swe2-max
```

→ creates a Devin Cloud session → opens a Discord thread → streams Devin's
messages in → pings you when it finishes, asks a question, or needs an
approval → anything you type in the thread steers the session.

## Feature summary

- **Session launch** — `/devin` with prompt, repo, title, mode, and model
- **Real model selection** — the full `/model` catalog (SWE-2 variants, Ultra,
  Opus 5.5, GPT-6.x Sol, Fusion…) via the internal ACP bridge, not just v3's
  five `devin_mode` tiers
- **Live relay** — Devin's messages stream into one thread per session
- **Phone-friendly notifications** — @-mentions on turn end / real questions /
  approval requests / failures; silent on routine suspensions
- **Steering** — any message in a bound thread forwards to the session
  (auto-resumes suspended sessions); attachments pass through as URLs
- **Buttons** — Open · Refresh · Approve · SSH instructions
- **Durable** — SQLite bindings survive restarts; allowlist-gated

## Quickstart

1. Discord app + bot token, private guild channel — [docs/setup.md](docs/setup.md)
2. Devin service-user `cog_` key + org ID
3. Optional but recommended: `devin auth login` for model selection
4. `cp .env.example .env`, fill in, `python -m devinmobile.bot.main`

## Docs

| Doc | Contents |
| --- | --- |
| [docs/setup.md](docs/setup.md) | Discord app, Devin credentials, `.env`, first run |
| [docs/usage.md](docs/usage.md) | Commands, threads, steering, notifications, model picker |
| [docs/architecture.md](docs/architecture.md) | Components, data flow, the two-API design |
| [docs/api-internals.md](docs/api-internals.md) | v3 endpoints used + the ACP bridge protocol (reverse-engineered) |
| [docs/deployment.md](docs/deployment.md) | Docker/VM deployment, credential mounting |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common failures and fixes |

Status notes and design decisions for contributors live in [AGENTS.md](AGENTS.md).

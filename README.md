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
- **Live relay** — Devin's messages stream into one thread per session;
  a "Working…" message tracks live tool calls via the ACP bridge, and
  replies stream token-by-token as they generate
- **One-tap controls** — reply-quoting (`re: "…"` context), emoji commands
  (👍 approve · 🔁 refresh/resend · ⏸️ park)
- **Phone-friendly notifications** — @-mentions on turn end / real questions /
  approval requests / failures / PR+CI transitions; silent on routine
  suspensions
- **Steering** — any message in a bound thread forwards to the session
  (auto-resumes suspended sessions); attachments pass through as URLs;
  links are fetched inline — GitHub URLs via the App (private repos work),
  other pages via readability extraction — so Devin reads them without
  relying on its own browser
- **PR cards** — when a session opens a PR, the thread gets a card with
  state + CI status and **Merge / Approve / Close** buttons (GitHub App)
- **PR loop, closed** — Merge/Approve/Close/Auto-merge buttons, CI-failure
  "Ask Devin to fix", review comments relayed back to the session
- **Automation** — `/schedule` recurring tasks (with canned `dep-audit` /
  `test-coverage` / `security-scan` recipes), `/devin-all` repo fan-out,
  `devin` label on a GitHub issue auto-spawns a session, `devin-review`
  on a PR spawns a review session whose findings can post back to GitHub
- **Playbook chains** — `/chain playbook:janitor|iterate` runs multi-phase
  Devin workflows (audit → fix → review → auto-merge; implement → review →
  apply → auto-merge). Each phase is its own session seeded with the last
  phase's findings; gates on `structured_output.proceed`, "exactly one PR",
  and a chain-wide ACU cap; mutating phases pause behind a stateless
  **Continue →** button (`auto:yes` runs everything). Restart-safe — a
  startup sweep resumes any chain that crashed mid-advance
- **Repo memory** — `/note` saves standing per-repo guidance that rides
  every future spawn's prompt (`/notes`, `/unnote`); sessions can also
  write learnings back via `structured_output.repo_notes`, so each run
  teaches the next
- **HTTP task intake** — `POST /task` (bearer-authed, `TASK_INTAKE_TOKEN`)
  on the webhook port lets Siri Shortcuts, Raycast, or scripts spawn
  sessions; the response's `thread_url` deep-links straight into the live
  Discord thread
- **Digest** — `/digest` and `/schedule kind:digest` roll up what Devin
  did in a window (grouped by outcome, per-session summary + ACU) from
  summaries persisted at completion — a local read, no API calls
- **Monitors** — `/schedule kind:monitor watch:https://…` (or
  `ci:owner/repo`) checks each interval and spawns a fix session only on
  a red edge — cooldown + still-running dedup keep a hard-down target to
  one session per window; green-after-red posts a recovery note
- **Attribution** — every spawn records `spawned_by`; completion/PR pings
  go to the owner instead of the whole allowlist (infra events still ping
  everyone), `/continue` transfers ownership, respawns/chains inherit it.
  `TASK_INTAKE_TOKENS` gives each HTTP client a name; `DEVIN_USER_MAP`
  creates sessions as the spawner's own Devin user
- **Inbox** — `/inbox` (and `/schedule kind:inbox every:1d`) is the
  morning triage card: sessions waiting on you, errored runs, chains
  paused at Continue→, red monitors, and open PRs with CI state — each
  row links into the thread where the buttons already live
- **Attachments everywhere** — `/devin`/`/continue`/`/chain` take a native
  `attachment:` file picker; `/task` accepts `attachments` URLs; and a DM
  photo (or voice note, Whisper-transcribed) with a caption spawns a
  session straight from your phone's share sheet — `devin: <task>` works
  in DM too
- **Resilience** — an errored session auto-respawns once as a seeded
  continuation (summary + files + error), and `/continue` chains a
  finished session's work into a fresh one
- **Guardrails** — per-task `/devin budget:` ACU caps (auto-park at 100%),
  global cap pings, silence watchdog, `/kill` parking, `/usage` burn
  rollup, voice-note steering via Whisper
- **Small-team layer** — opt-in via env: `REQUIRED_ROLE_ID` guild-role
  access, `GITHUB_USER_MAP` sender attribution, `USER_ACU_DAILY` per-user
  ACU quotas, `TEAM_ADMIN_IDS` owner-or-admin gates on destructive ops,
  `HUB_CHANNEL_MAP` per-user thread lanes, `/allow` `/deny` runtime
  allowlist, `/inbox mine:`
- **Session admin** — `/kill` reports what the v3 `DELETE` actually did,
  `/delete` hides a session from lists (ACU still counts; `wipe:` clears
  the thread), `/rename` retitles the thread + binding
- **Secrets + playbooks** — `POST /secret` intake creates org secrets
  (values go HTTPS→bot→Devin, never Discord; org secrets auto-inject into
  every session's env), `/task secrets:` injects session-scoped env vars,
  `/playbook-save` `/playbook-run` `/playbooks` `/playbook-delete` manage
  Devin's stored runbooks
- **Presence** — the bot's Discord status shows `N Devin sessions running`
- **Diff-on-phone** — completion cards attach the PR's `.diff` file so the
  actual change is readable without opening GitHub
- **Issue/branch intake** — `/devin issue:#42 branch:feat-x` feeds GitHub
  context into the prompt
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

# Using the bot

One Discord thread = one Devin session. Everything happens in the thread.

## Commands

### `/devin`

Creates a session and its thread.

| Option | Type | Notes |
| --- | --- | --- |
| `prompt` | required | The task, verbatim — it's the first user message to Devin |
| `model` | optional, autocomplete | Real model picker: `swe2-max`, `opus`, `ultra`, `fusion`, `lite`, `gpt-6.1`, … Free text is fuzzy-matched against the server's live catalog; unknown values error back with the valid list |
| `repo` | optional, autocomplete | Suggests your connected `owner/repo`s (comma-separated multi-select works — autocomplete applies to the last token). Free text is fuzzy-matched and canonicalized; unknown repos error back with the valid list |
| `issue` | optional | GitHub issue to attach as context: URL, `owner/repo#n`, or bare `#n` (resolved against `repo:`). Title + body are fetched and appended to the prompt. Needs the GitHub App |
| `branch` | optional | Base branch for the work — adds a "checkout from origin/branch" line to the prompt |
| `mode` | optional | v3 `devin_mode` tier — `lite`/`normal`/`fast`/`ultra`/`fusion`. Ignored when `model:` is set (the model select supersedes it). Beats `DEVIN_DEFAULT_MODEL` when passed explicitly |
| `title` | optional | Thread/session title; defaults to the prompt (or the issue title when `issue:` is set) |
| `budget` | optional | Per-task ACU cap. At 100% the session is parked on our side (and v3's own `max_acu_limit` stops it server-side on the v3 path). Overrides `MAX_ACU_LIMIT` for this session's 80%/100% pings |

`DEVIN_DEFAULT_MODEL` (env) applies a model to every `/devin` call that passes
neither `model:` nor `mode:`. Option order in Discord is fixed — required
first — so `prompt` always leads; `model` is the first optional chip.

`model:` sessions are created over the ACP bridge as *your* Devin user (see
[setup](setup.md#3-devin-cli-login-model-selection)); they don't get
`tags`, `structured_output_schema`, or `MAX_ACU_LIMIT` — those only exist on
the plain v3 path.

### `/devin-all`

Fan-out: one session per repo, one thread each.

| Option | Type | Notes |
| --- | --- | --- |
| `prompt` | required | The task, run once per repo |
| `repos` | required, autocomplete | Comma-separated `owner/repo` list (2+) |
| `model` / `mode` / `title` | optional | Same semantics as `/devin`; `title` becomes a prefix (`title · repo`) |

### `/schedule`

Recurring sessions — the task queue that runs while you sleep.

| Option | Type | Notes |
| --- | --- | --- |
| `every` | required | `30m`, `6h`, `1d`, … (min 5m) |
| `prompt` | optional | What Devin does each run — or pick a `recipe:` |
| `recipe` | optional, choice | Canned maintenance loop: `dep-audit` (outdated + vulnerable deps → upgrade PR), `test-coverage` (coverage gaps → test PR), `security-scan` (scanners + secrets/auth audit → severity report). A `prompt:` given alongside is appended to the recipe |
| `repo` | optional, autocomplete | Repo(s) for each run |
| `model` | optional | Model alias (bridge) |

Rows persist in SQLite and survive restarts. A downtime doesn't
catch-fire the backlog — `next_run_at` slides forward from the actual fire
time.

### `/continue`

Run inside a finished/errored/suspended session's thread: spawns a fresh
session seeded with the old one's `structured_output` summary, files
touched, repo(s), model, and budget — plus your `notes:` if given. The old
thread links to the new one. Sessions still `running` are refused — reply
to steer instead.

### `/schedules` / `/unschedule <id>`

List recurring tasks (next run, interval, state) / delete one.

### `/sessions`

Lists the last 10 sessions this bot started — status, model, ACU burn,
and a link to each thread.

### `/kill [session_id]`

Parks a session: stops tracking, archives the thread, posts a marker.
Defaults to the current thread's session when run inside one. Also tries a
v3 `DELETE` (undocumented — best-effort to stop ACU burn sooner).

### `/usage`

ACU burn rollup across sessions this bot started: today / last 7d /
all-time, top 5 sessions by burn, and a per-repo breakdown. Day buckets
group by session *start* (ACU is cumulative per session, not per day).

### `/devin-status [session_id]`

Refreshes a session's status embed on demand (defaults to the most recent).

## The thread

- **Anchor embed** at the top: status (`running — working`, …), model/mode,
  ACU burn, PR links. The **Refresh** button updates it in place.
- **Relayed messages**: Devin's replies post as normal messages, chunked at
  ~1900 chars. File attachments arrive as `Attachment: [name](url)` links.
- **Steering**: type anything → forwarded via `POST /messages` → ✅ reaction
  = delivered, ❌ = failed. A suspended session auto-resumes on message.
  Discord attachments ride along as `attachment_urls`.
- **Reply-quoting**: reply to a Devin message → its text is prepended as
  `re: "…"` so "yes" / "that one" carry context. Works on embeds too
  (anchor and PR cards resolve to their title/link).
- **Emoji commands** (in a bound thread): 👍 on the anchor = approve the
  pending step; 👍 on a PR card = GitHub approve; 🔁 on the anchor = poll
  now; 🔁 on a PR card = refresh its state; 🔁 on a ❌-marked message =
  resend it; ⏸️ on the anchor = park (any reply resumes).
- **Live replies**: when the ACP stream is attached, Devin's text streams
  into a message as it generates (~1.2s edits); the canonical message
  replaces the preview when it lands via the poll — no duplicates.
- **Link reading**: URLs in a steering message are fetched by the bot and
  appended to the prompt as `Link context` blocks — Devin reads the text
  instead of having to browse. `github.com` issue/PR/commit/blob links go
  through the GitHub App (private repos included); everything else is a
  plain GET + readability extraction. Caps: 3 links/message, ~3k chars
  each. Unreadable links are noted so Devin knows to try its own browser.
- **Liveness**: the bot shows "typing…" while the session is mid-turn, and
  (when the bridge is available) a single **"Working…"** message tracks the
  live tool calls — `Read /path/x.py`, `Run pytest -x` — edited in place
  every ~3s and deleted when the turn ends. The transcript stays clean;
  set `ACP_PROGRESS=0` to disable.
- **Voice steering**: with `OPENAI_API_KEY` set, a voice message in a bound
  thread is transcribed (Whisper) and sent to the session as text — the
  transcript echoes back as a quote so you can see what was sent.
- **Silence watchdog**: a session that's `running` but quiet for
  `SILENCE_ALERT_MINUTES` (default 20) posts one quiet note — no ping.
- **Auto-respawn**: when a session transitions to `error`, a continuation
  session spawns automatically in a new thread — seeded with the dead
  session's summary, files touched, and error detail. One deep only
  (a respawned session won't respawn again). `AUTO_RESPAWN=0` disables.
- **Presence**: the bot's Discord status shows how many sessions it's
  actively tracking (`watching N Devin sessions`) — glanceable "is it
  doing anything" without opening a thread.

## Buttons (anchor embed)

| Button | Does |
| --- | --- |
| Open in Devin | Link to `app.devin.ai/sessions/<id>` |
| Refresh | Re-fetch status, edit the embed |
| Approve | Sends "Approved — please proceed." as a session message (works when Devin's safe mode accepts message approvals) |
| SSH | Ephemeral reply with `ssh <id>@ssh.devin.ai` — Cognition's gateway; open the printed URL to approve. Use Blink/Termius on a phone for a real shell |

## PR cards

When a session opens a PR, a card posts into its thread (requires the
[GitHub App](setup.md#4-github-app-pr-actions-ci-issue) — without it, PRs
still appear as plain links in the anchor embed):

- Title, state icon, and CI rollup (✅/❌/🟡) in the embed — edited in place
  as state changes
- **Open on GitHub** — link button
- **Merge** — asks for an ephemeral confirmation first, then merges with
  `GITHUB_MERGE_METHOD` (default squash)
- **Approve** — posts an `APPROVE` review as the GitHub App
- **Auto-merge** — opt-in toggle: the relay merges the moment CI reports
  green. Off by default; the card footer shows the flag
- **Close PR** — closes without merging

State transitions (opened → merged/closed, CI green → red) also post into
the thread; CI failures and merges ping. A **CI failing ❌** notice carries
an **Ask Devin to fix** button that forwards the failing check names back
to the session.

With webhooks on, PR review comments post into the thread too, with a
**Send to Devin** button that feeds the feedback back to the session —
the whole review → fix → merge loop stays on the phone.

## Label-triggered sessions

With the webhook receiver enabled, adding the `devin` label
(`GITHUB_TRIGGER_LABEL`) to a GitHub issue spawns a session automatically —
the issue title+body become the prompt, the repo is attached, and the new
thread notes where it came from. Fill the issue tracker from anywhere;
Devin picks the work up.

Adding the `devin-review` label (`GITHUB_REVIEW_LABEL`) to a **pull
request** spawns a review session instead: Devin reads the diff and posts
findings in its thread as a numbered file:line list. One session per PR —
re-labeling is a no-op, and bot-authored PRs are ignored. The completion
card carries a **Post review to GitHub** button that publishes the
findings as a `COMMENT` review (never APPROVE — merging stays human).

## Notifications

@-mention = phone push. Non-mentions post quietly in the thread.

| Event | Ping? | Text |
| --- | --- | --- |
| `waiting_for_user`, last msg ends with `?` | ✅ | `Devin is asking: "{question}"` |
| `waiting_for_user`, otherwise | ✅ | `Devin finished its turn — reply here to continue.` |
| `waiting_for_approval` | ✅ | `Devin needs an approval — tap Approve or open the session.` |
| `exit` / `finished` | ✅ | `Session finished.` + completion embed (structured summary, files, tests when available) + `.diff` file when the session produced exactly one PR |
| `error` | ✅ | `Session errored.` (then auto-respawn posts a continuation link) |
| `suspended` (`out_of_credits`/`out_of_quota`/`usage_limit_exceeded`) | ✅ | `Session suspended (…) — needs attention.` |
| `suspended` (`inactivity`/`user_request`) | ❌ | `Session suspended (…) — reply here to resume it.` |
| PR merged / closed | ✅ / ❌ | `PR #n merged` / `PR #n closed.` |
| PR CI → success | ✅ | `PR #n — CI green ✅` |
| PR CI → failure | ✅ | `PR #n — CI failing ❌` (with Fix-CI button) |
| ACU ≥80% / ≥100% of cap (`budget:` or `MAX_ACU_LIMIT`) | ✅ | `ACU usage is nearing/hit the cap: N of M ACUs` — at 100% the session parks |
| running + silent > `SILENCE_ALERT_MINUTES` | ❌ | `Devin has been quiet for ~Nmin — likely still working.` |

Notification texts live in `classify_transition` in `src/devinmobile/relay.py`.

## Model values

The picker resolves (in order): alias → exact catalog value → exact catalog
name → unique substring. Aliases (`acp_bridge.MODEL_ALIASES`):

`fusion`/`auto`, `ultra`, `normal`/`default`, `fast`, `lite`,
`swe`/`swe2`/`max`/`swe2-max`, `swe2-high`/`high`, `swe2-medium`/`medium`,
`*-priority` variants, `opus`/`opus-5.5`, `gpt-6`, `gpt-6.1`, `gpt-5.6`.

Raw catalog values (`devin-swe-2-priority-max`, …) always work — the list is
validated against what the server advertises at create time, so new models
are usable the day they ship.

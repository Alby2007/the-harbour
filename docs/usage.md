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
| `attachment` | optional | Photo/file to seed the session with (screenshot, log, spec). Sent to v3 as `attachment_urls`; on `model:` bridge spawns the URL is inlined into the prompt instead |

`DEVIN_DEFAULT_MODEL` (env) applies a model to every `/devin` call that passes
neither `model:` nor `mode:`. Option order in Discord is fixed — required
first — so `prompt` always leads; `model` is the first optional chip.

`model:` sessions are created over the ACP bridge as *your* Devin user (see
[setup](setup.md#3-devin-cli-login-model-selection)); they don't get
`tags`, `structured_output_schema`, or server-side `MAX_ACU_LIMIT` — those
only exist on the plain v3 path. `budget:` still works on bridge sessions —
the bot parks the binding when burn crosses it.

### `/devin-all`

Fan-out: one session per repo, one thread each.

| Option | Type | Notes |
| --- | --- | --- |
| `prompt` | required | The task, run once per repo |
| `repos` | required, autocomplete | Comma-separated `owner/repo` list (2+) |
| `model` / `mode` / `title` | optional | Same semantics as `/devin`; `title` becomes a prefix (`title · repo`) |
| `attachment` | optional | Photo/file attached to **every** spawned session |

### `/schedule`

Recurring sessions — the task queue that runs while you sleep.

| Option | Type | Notes |
| --- | --- | --- |
| `every` | required | `30m`, `6h`, `1d`, … (min 5m) |
| `prompt` | optional | What Devin does each run — or pick a `recipe:` |
| `recipe` | optional, choice | Canned maintenance loop: `dep-audit` (outdated + vulnerable deps → upgrade PR), `test-coverage` (coverage gaps → test PR), `security-scan` (scanners + secrets/auth audit → severity report). A `prompt:` given alongside is appended to the recipe |
| `repo` | optional, autocomplete | Repo(s) for each run |
| `model` | optional | Model alias (bridge) |
| `kind` | optional, choice | `spawn` (default) runs the prompt; `digest` posts the window rollup; `inbox` posts the triage card; `monitor` checks first and only spawns your `prompt:` when the check goes red |
| `watch` | monitor only | What to check each interval: `https://…` URL (non-2xx, unreachable, or missing `expect:` = red) or `ci:owner/repo[@branch]` (CI rollup — needs the GitHub App) |
| `expect` | monitor only | Substring the URL body must contain (URL watches only) |
| `cooldown` | monitor only | Still-red re-fire floor, `30m`/`4h`/`1d` syntax (default 4h). A red edge always fires once; a persistently-down target refires at most once per cooldown, and never while its previous fix session is still running |

`kind:monitor` turns the scheduler into a monitoring layer — the spawn is
"session investigates", not "page a human". A transport failure and a
bot-host network outage both read red (bounded to one spawn per edge); a
GitHub API failure on a `ci:` watch reads `unknown` and never spawns.
Green-after-red posts a ✅ recovery line to the hub channel.

Rows persist in SQLite and survive restarts. A downtime doesn't
catch-fire the backlog — `next_run_at` slides forward from the actual fire
time.

### `/digest`

`/digest hours:N` (default 24, max 168) — an ephemeral embed grouping the
window's sessions into Completed / Errored / Suspended / In flight, each
row linked to its thread with the captured `structured_output` summary
and ACU spend; footer totals the window. Summaries persist on the binding
at completion, so no API calls — sessions that finished before this
feature landed show title-only. A session counts if it had activity in
the window or is still polling — a long silent run doesn't fall out.
A session active across two windows appears in both.

### `/inbox`

The prospective counterpart to `/digest` — a triage card of everything
waiting on a human, urgency-ordered:

1. **Waiting on you** — sessions at `waiting_for_user`/`waiting_for_approval`
   (question excerpts when the last message ends in `?`)
2. **Errored** — sessions that died without producing a continuation
3. **Chains awaiting Continue →** — human-gated playbook phases
4. **Monitors red** — `kind:monitor` schedules currently failing
5. **Open PRs** — tracked PRs with CI state (+ `armed` when auto-merge
   is on)

Each row links to its thread — the action buttons already live there.
Empty → "inbox zero" plus up to two idle-repo suggestions (repos we know
from notes/PRs with no session activity in 7d). `/schedule kind:inbox
every:1d` posts the card to the hub channel daily — the morning
touchpoint.

`/inbox mine:yes` filters the session-derived sections (waiting, errored,
pending chains) to things *you* spawned — monitors and open PRs stay
shared since they're team infra, not a personal queue.

### `/continue`

Run inside a finished/errored/suspended session's thread: spawns a fresh
session seeded with the old one's `structured_output` summary, files
touched, repo(s), model, and budget — plus your `notes:` if given. The old
thread links to the new one. Sessions still `running` are refused — reply
to steer instead. `attachment:` seeds the continuation with a fresh
screenshot/file.

### `/chain`

Runs a **playbook**: a named, ordered workflow where each phase is its own
Devin session (a `continued_from` child seeded with the previous phase's
structured output) or a bot-side action. One thread per phase; each
completion card links to the next.

| Option | Type | Notes |
| --- | --- | --- |
| `playbook` | required, choice | `janitor` or `iterate` — see below |
| `prompt` | required | The task; becomes phase 0's prompt (`{orig}` for later phases) |
| `repo` | optional, autocomplete | Same semantics as `/devin` — inherited by every phase |
| `budget` | optional | **Chain-wide** ACU cap rolled across all phases (default: `MAX_ACU_LIMIT` × phase count) |
| `auto` | optional | `yes` runs every phase without asking; default pauses before mutating phases |
| `title` | optional | Thread title prefix (`title · phase-name`) |
| `attachment` | optional | Photo/file — rides into the phase-0 spawn only |

**Playbooks**

- **`janitor`** — `audit → fix → review → automerge`. Audit sweeps the repo
  for dead code / vulnerable deps / failing tests / coverage gaps and
  reports findings (no fixes). If it reports `proceed=false` ("repo is
  clean"), the chain halts there. Fix is human-gated — a **Continue → fix**
  button on the audit's completion card spawns it. Review runs only when
  fix produced exactly one PR, then `automerge` arms that PR's Auto-merge
  flag bot-side (no session) so it merges itself when CI greens.
- **`iterate`** — `implement → review → automerge` … plus an `apply` phase:
  implement opens a PR, review examines the diff, then **Continue → apply**
  spawns a fixer that pushes review fixes to the same PR (skipped entirely
  if the review reports `proceed=false` — "PR is clean"), then auto-merge
  arms.

**Gates.** Each phase transition checks (in order): the next phase exists;
its gate passes against the just-finished session (`proceed` reads the
session's `structured_output.proceed` — an explicit `false` SKIPS that
phase and re-evaluates the next, so a clean iterate review still reaches
automerge; `single_pr` requires exactly one tracked PR); and the chain's
ACU cap hasn't been spent. A failed gate or running off the end posts
`⛓️ chain <name> done — <reason>` and the chain ends there.

Both playbooks need the GitHub App — without it the PR gate can't see
what Devin opened, so `/chain` refuses to start when GITHUB_APP_* isn't
configured.

**Human gates.** A non-auto phase posts a `Continue → <phase>` button on
the completion card. Tapping it force-advances the chain and swaps the
button out. The button is stateless — it survives bot restarts; if the
chain already moved on, the tap answers ephemerally instead of double-
spawning.

**Restart safety.** Chain state lives on each phase's `bindings.chain` JSON
(no separate table): `playbook`, `step`, `pending`, `cap`, `spent`,
`pr_key`, `orig`, `auto`. On startup a resume sweep re-runs the advance for
any exited chain phase that has no continuation child yet — `child_of`
dedupes a spawn that landed right before a crash. `/kill` on a phase's
thread parks that binding; the chain halts with it (a parked session can't
complete, so nothing advances).

If a phase's session *errors*, the chain halts with a notice — auto-respawn
only covers phase 0 (continuation depth caps at 1). `/continue` in that
thread retries the phase with the chain intact: it clears `pending`/`halted`
and rolls the dead session's ACUs into `spent` so the budget still holds.

### `/chains`

Ephemeral list of the last 25 chain bindings: playbook, `step/total` +
phase name, ⏸ marker while a Continue→ is pending, cumulative ACU spend,
and a link to each phase's thread.

### `/note` `/notes` `/unnote <id>`

Repo memory — standing notes that get injected into every future session's
prompt for that repo (spawn-time, so `/devin`, `/schedule`, `/chain` phases,
respawns, and label-triggered spawns all see them):

- `/note text:<note> [repo:]` — save guidance like "tests are flaky — run
  `pytest -x`" or "use pnpm, not npm". Inside a session thread `repo:`
  defaults to that session's repo; elsewhere it's required.
- `/notes [repo:]` — ephemeral list (`#id · repo · text`, newest 20).
- `/unnote id:N` — delete by id (owner-or-admin when `TEAM_ADMIN_IDS`
  is configured, same as `/unschedule`).

Caps: 8 notes per repo (newest win), ~2k chars per injected block. The
write-back half is automatic — when a session completes with
`structured_output.repo_notes` populated, those entries save as
`devin:<session>` notes (deduped on `(repo, note)`) and the thread gets a
`📝 Saved N repo notes` line. So each session can teach the next.

### `/schedules` / `/unschedule <id>`

List recurring tasks (next run, interval, state) / delete one. Deleting
someone else's schedule needs owner-or-admin when `TEAM_ADMIN_IDS` is
configured.

### `/sessions`

Lists the last 10 sessions this bot started — status, model, ACU burn,
and a link to each thread.

### `/kill [session_id]`

Parks a session: stops tracking, archives the thread, posts a marker.
Defaults to the current thread's session when run inside one. Also tries a
v3 `DELETE` (undocumented — best-effort to stop ACU burn sooner). When
`TEAM_ADMIN_IDS` is configured, parking *someone else's* session needs
owner-or-admin — marker-spawned sessions (`github`, `intake`) stay
shared.

### `/allow <user>` / `/deny <user>`

Runtime allowlist — grants/revokes operator access without an env edit or
restart (the `allowed_users` db table unions with `ALLOWED_USER_IDS` and
`REQUIRED_ROLE_ID` at the gate). Both commands refuse unless
`TEAM_ADMIN_IDS` is configured **and** the caller is in it — without
admins, operators could self-escalate. `/deny` only removes the runtime
row; env-allowlisted or role-based access isn't affected.

### `/usage`

ACU burn rollup across sessions this bot started: today / last 7d /
all-time, top 5 sessions by burn, per-repo breakdown, and a per-user
breakdown (`spawned_by` — Discord pings for snowflakes, marker names like
`github`/`intake` as plain text). Day buckets group by session *start*
(ACU is cumulative per session, not per day).

### `/devin-status [session_id]`

Refreshes a session's status embed on demand (defaults to the most recent).

## HTTP task intake

`POST /task` on the webhook port lets anything that can curl spawn a
session — Siri Shortcuts, Raycast, another bot, a cron script. Discord
becomes the renderer rather than the only intake source.

Set `TASK_INTAKE_TOKEN` to enable (empty = route off). The port is the
same `GITHUB_WEBHOOK_PORT`; the bearer token is the whole auth story, so
keep it behind the same tunnel/firewall as the webhook secret.

```bash
curl -X POST https://<host>/task \
  -H "Authorization: Bearer $TASK_INTAKE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "fix the flaky login test", "repo": "org/repo", "budget": 5}'
```

Body: `prompt` (required, ≤4000 chars), `repo` (string or array,
comma-separate works), `title`, `budget` (ACU cap), `attachments` (array
of ≤8 `http(s)://` URLs, each ≤2048 chars — they must be publicly
fetchable by Devin), `by` (optional caller name ≤64 chars — becomes the
session's `spawned_by`, so a Discord snowflake pings that user).
Success → `200` with `session_id`, `thread_id`, `session_url`, and
`thread_url` — the Discord deep-link is what makes a Shortcut useful
(tap → lands in the live thread). Errors: `401` bad/missing token, `400`
bad body, `502` spawn failure.

Per-client tokens: `TASK_INTAKE_TOKENS=abc:alby,def:sam` gives each caller
its own bearer — requests authed by a mapped token attribute the session
to the mapped name and **ignore** `by:` (the mapping is the stronger
claim). Either mechanism alone enables the route; both can coexist.

## DM intake

The bot's DM is itself an intake surface — the lowest-friction phone path:

- **Photo/file + caption** — the caption is the prompt; the file rides as
  `attachment_urls`.
- **Photo/file, no caption** — a default "analyze these and report what
  you find" prompt.
- **`devin: <task>`** — text-only spawn (case-insensitive prefix).
- **Voice note** — transcribed via Whisper into the prompt (needs
  `OPENAI_API_KEY`; without it the bot replies why). A caption merges
  with the transcript (`transcript + caption`); other attachments on the
  same message still ride along. Without a key, a captioned voice note
  spawns from the caption alone and the bot says the audio was skipped —
  a bare `.ogg` URL is useless to Devin, so it's never attached.
- **Anything else** — a one-line hint, no spawn. Stray chatter stays
  inert, and non-allowlisted senders get silence.

The reply links into the spawned hub-channel thread (`thread_url`). DMs
can't host threads — that's why the session still lands in the hub.

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
- **Choice buttons**: when a relayed Devin message is a question with an
  enumerated option list (`1.`/`2.`/…, `A)`/`B)`/…, or bullets), the
  message gets one button per option — a tap sends that choice to the
  session verbatim, removes the buttons, and annotates the message with
  `*(answered: …)*`. Confirmation questions ("should I proceed?", "want
  me to continue?") get plain **Yes**/**No** buttons. Ambiguous or
  oversized lists get no buttons — answering by typing always works.
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
  live tool calls — `` `Read /path/x.py` ``, `` `$ pytest -x` `` (shell
  commands get prompt styling) — edited in place every ~3s. Each call's
  line gains `✓`/`✗` as it finishes, and failures append a
  `↳ last output line` tail. The message is deleted when the turn ends;
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

@-mention = phone push. Non-mentions post quietly in the thread. Mentions
route through `spawned_by`: sessions a Discord user spawned ping **that
user only**; sessions spawned by infra (GitHub labels → `github`, `/task`
without a mapped token → `intake`, legacy rows → unset) ping the whole
allowlist — team events nobody in particular owns. Ownership transfers on
`/continue` (the tapper gets the pings) and **inherits** on auto-respawn
and chain phases (a Continue→ tap doesn't re-own a playbook).

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

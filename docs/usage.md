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

`DEVIN_DEFAULT_MODEL` (env) applies a model to every `/devin` call that passes
neither `model:` nor `mode:`. Option order in Discord is fixed — required
first — so `prompt` always leads; `model` is the first optional chip.

`model:` sessions are created over the ACP bridge as *your* Devin user (see
[setup](setup.md#3-devin-cli-login-model-selection)); they don't get
`tags`, `structured_output_schema`, or `MAX_ACU_LIMIT` — those only exist on
the plain v3 path.

### `/sessions`

Lists the last 10 sessions this bot started, with status and a link to each
thread.

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
- **Close PR** — closes without merging

State transitions (opened → merged/closed, CI green → red) also post into
the thread; CI failures and merges ping.

## Notifications

@-mention = phone push. Non-mentions post quietly in the thread.

| Event | Ping? | Text |
| --- | --- | --- |
| `waiting_for_user`, last msg ends with `?` | ✅ | `Devin is asking: "{question}"` |
| `waiting_for_user`, otherwise | ✅ | `Devin finished its turn — reply here to continue.` |
| `waiting_for_approval` | ✅ | `Devin needs an approval — tap Approve or open the session.` |
| `exit` / `finished` | ✅ | `Session finished.` + completion embed (structured summary, files, tests when available) |
| `error` | ✅ | `Session errored.` |
| `suspended` (`out_of_credits`/`out_of_quota`/`usage_limit_exceeded`) | ✅ | `Session suspended (…) — needs attention.` |
| `suspended` (`inactivity`/`user_request`) | ❌ | `Session suspended (…) — reply here to resume it.` |
| PR merged / closed | ✅ / ❌ | `PR #n merged` / `PR #n closed.` |
| PR CI → success | ✅ | `PR #n — CI green ✅` |
| PR CI → failure | ✅ | `PR #n — CI failing ❌` |

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

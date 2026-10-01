# API internals

What this project actually calls, including the undocumented bridge. Verified
against `api.devin.ai` and devin CLI `3000.10.48` (bundled with Devin
Desktop) — the ACP details are empirical and could shift between releases.

## v3 REST — `https://api.devin.ai/v3`

`Authorization: Bearer cog_...`. All paths are `/organizations/{org_id}/...`.

| Call | Used for | Notes |
| --- | --- | --- |
| `POST /sessions` | create | `prompt` required; `repos[]`, `devin_mode`, `title`, `tags[]`, `max_acu_limit`, `create_as_user_id`, `structured_output_schema`, `attachment_urls`, `bypass_approval`. **Unknown fields are silently ignored** — no strict-schema 422 |
| `GET /sessions/{id}` | status | `status`: `new claimed running exit error suspended resuming`; `status_detail`: `working waiting_for_user waiting_for_approval finished inactivity user_request usage_limit_exceeded out_of_credits out_of_quota error`; plus `pull_requests[]`, `structured_output`, `tags`, `devin_mode`, `user_id`, `acus_consumed` |
| `GET /sessions/{id}/messages?first=N[&after=cursor]` | relay | Chronological `{event_id, source: devin\|user, message, created_at}`. `after` only accepts the response's own `end_cursor` (event ids rejected); `end_cursor` is null on the last page → refetch + local dedupe |
| `POST /sessions/{id}/messages` | steer | `{"message": ...}`; auto-resumes suspended sessions; supports `attachment_urls`, `message_as_user_id`. Needs `ManageOrgSessions` |
| `GET /sessions` | list | Response is `{"items": [...]}`. Only lists v3-created sessions — bridge/Desktop/CLI sessions appear only after their first prompt |
| `GET /organizations/{org}/members` | find `user-...` for attribution | Requires `ViewOrgMembers`-ish scope; works with Admin service key |

Devin messages can embed `ATTACHMENT:{json}` markers inline — the relay strips
them and relays `url` as a link.

## ACP bridge — `wss://{devin_api_url}/acp/live?token={windsurf_api_key}`

JSON-RPC 2.0, one message per WS text frame. This is what the Devin CLI
(`devin --cloud` in docs, `/cloud` `/handoff` in the REPL) and Devin Desktop
use to create and drive cloud sessions. The binary confirms the flow:
`session/new` → `set_config_option` → `session/prompt`, then
`session/update` notifications stream the work.

### Auth

`devin auth login` (browser OAuth, `--force-manual-token-flow` for headless)
writes `~/.local/share/devin/credentials.toml`:

```toml
windsurf_api_key = "devin-..."   # long-lived user credential -> token= param
api_server_url   = "https://server.codeium.com"
devin_webapp_host = "app.devin.ai"
devin_api_url    = "https://api.devin.ai"   # -> wss://.../acp/live
```

The service key (`cog_`) is rejected (403 on handshake). The `devin-` user
key authenticates the connection; no ACP `authenticate` call is needed
(`initialize` returns `authMethods: []`).

### Wire flow

```jsonc
// -> initialize
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
  "protocolVersion":1,
  "clientCapabilities":{"fs":{"readTextFile":false,"writeTextFile":false},
                        "terminal":false},
  "clientInfo":{"name":"devinmobile","version":"0.1.0"}}}
// <- result: agentInfo{devin 1.0.0}, agentCapabilities{loadSession:true,
//    sessionCapabilities{list:{}}, ...}, authMethods:[]
```

```jsonc
// -> session/new
{"id":2,"method":"session/new","params":{"cwd":"/","mcpServers":[]}}
// <- result.sessionId: "devin-<hex>"   (draft — not v3-visible yet)
//    result.configOptions: config selects (below)
```

`configOptions` observed — `{id, name, type:"select", currentValue, options:[{name,value}]}`:

| id | Purpose | Observed values |
| --- | --- | --- |
| `org_id` | Owning org | `org-...` |
| `repos` | Repos to clone | `owner/name` per option |
| `persona_slug` | Persona | `""` (Agent), `dana` (Data Analyst) |
| `devin_version` | **The model picker** | see below |
| `platform` | Session OS | `linux` `macos` `windows` |

`devin_version` catalog (Sept 2026 — resolve against the live list, don't
hardcode): `devin-auto` Fusion · `devin-ultra` Ultra · `devin-2-5` Normal ·
`devin-fast-opus` Fast · `devin_lite` Lite · `devin-swe-2-low|high|max`
SWE-2 Medium/High/Max · `devin-swe-2-priority-low|high|max` Priority
variants · `devin-opus-5-5` Opus 5.5 · `devin-gpt-6-sol`,
`devin-gpt-6-1-sol`, `devin-gpt-5-6` GPT Sol builds.

```jsonc
// -> session/set_config_option
{"id":3,"method":"session/set_config_option","params":{
  "sessionId":"devin-...","configId":"devin_version","value":"devin-swe-2-max"}}
// <- result.configOptions (full updated list; currentValue reflects the set)
//
// ⚠ set_config_option values are validated against the option list and
//   non-matching values are SILENTLY DROPPED (currentValue returns '').
//   No error. Always send the canonical `value` — resolve user input
//   case-insensitively against options first (resolve_option does this).
//
// ⚠ `repos` requires a snapshot blueprint for the repo or the workspace
//   won't clone it (the option registers on sessionRepos metadata, but the
//   VM falls back to the default repo). Mirror the CLI: ensure a blueprint
//   exists before prompting —
{"id":4,"method":"_cognition.ai/snapshot-setup/list-blueprints",
 "params":{"org_id":"org-..."}}
{"id":5,"method":"_cognition.ai/snapshot-setup/create-blueprint",
 "params":{"org_id":"org-...","repo_name":"Alby2007/Stockbot"}}   // if missing
//
// Probed (scripts/probe_snapshots.py): blueprints carry
// blueprint_id / repo_name / current_version_id(+source) — versions are
// created server-side ("env_suggestion"). There is NO snapshot/blueprint
// select in session/new's configOptions and no get/update/save methods —
// blueprints auto-apply per repo; you can't pin a snapshot at create time.

// -> session/prompt   ⚠ response arrives at TURN END (minutes) — don't wait
{"id":4,"method":"session/prompt","params":{
  "sessionId":"devin-...","prompt":[{"type":"text","text":"..."}]}}
// <- meanwhile: {"method":"session/update","params":{"sessionId":...,
//    "update":{...}}} notifications stream progress (also user_message echo,
//    availableCommands, token usage, status/lifecycle meta under
//    cognition.ai/* keys). tool_call/tool_call_update are upserts keyed by
//    toolCallId — kind (execute/read/edit/…), status
//    (pending/in_progress/completed/failed), title, rawInput{command,path},
//    content[], locations[]; updates patch the call, not append
// <- result.stopReason "end_turn" + _meta {url, sessionStatus, orgId, ...}
```

Also present in the binary's method table: `session/load` (resume + replay),
`session/list`, `session/delete`, `session/set_mode`, plus a `cognition.ai/*`
extension namespace (sessionRename, sessionArchiving, queuedMessages,
sessionHeartbeat, userShellCommand, …).

### Probed extensions (scripts/probe_ext.py, Oct 2026)

The binary's table overstates what the bridge build actually routes. Live
results against a throwaway session:

| Surface | Wire result |
| --- | --- |
| `session/list` | **Works** — `{"sessions": [{"_meta": {createdAt, creatorUserId, url, orgId}, ...}]}` |
| `session/load` | Works — response keys `[_meta, configOptions]`; **no** `SessionModeState` |
| `session/set_mode` | `Method not found` (-32601); `setMode`/`setSessionMode` same; `set_config_option configId="mode"` → `Unknown configId` (-32602). No `availableModes`/`currentModeId` anywhere → **no per-session mode switching on this build** |
| `cognition.ai/userShellCommand` | **Advertised** `true` in `initialize` `agentCapabilities._meta` but unreachable: rejected as a `prompt` content block (-32602 — the prompt union only accepts text/image/audio/resource_link/resource), `-32601` as a standalone method (also `_`-prefixed and `shellCommand` spellings), and **silently ignored** as a top-level `session/prompt` param (`userShellCommand` / `cognition.ai/userShellCommand`), inside a text block's `_meta`, as request `_meta`, and as a JSON-RPC notification. `echo` marker never appeared in stream updates or the v3 transcript. The capability flag exists; the route doesn't — likely gated to Cognition's own clients |
| `cognition.ai/queuedMessages`, `cognition.ai/sessionRename`, `sessionHeartbeat`, `session/resume`, `session/close` | All `Method not found` (-32601) — `sessionRename` is likewise advertised `true` in capabilities yet unrouted |

Consequence: `/exec` and `/mode` are **not** built — a user shell needs an
agent turn (normal steering text), and there is no mode surface to switch.
If a future CLI version ships these, re-run the probe; the capability flags
in `initialize` are the first tell.

### Draft → materialization

A `session/new` with no prompt is a draft: invisible to `GET /v3/.../sessions`
and 403s on item GET — even for the owner's token. The first
`session/prompt` commits it into the org session store; afterwards the v3
service key can `GET`, list, poll messages, and steer it. The bare hex id
(`devin-` prefix stripped) is what v3 lists and what
`app.devin.ai/sessions/<id>` uses; the prefixed form also resolves on GET.

### Caveats

- Killing the WS mid-turn is safe — the session runs server-side
  ("closing the terminal does not stop it" is literal).
- `session/prompt` holding the JSON-RPC response until turn end is why
  `acp_bridge._rpc(..., early_update_for=session_id)` exists: the first
  `session/update` proves acceptance, then we detach.
- Bridge sessions are the *user's* sessions — no `create_as_user_id`
  needed, and org Admin service keys can still manage them via v3 once
  materialized.
- No bridge equivalent found for `tags`, `structured_output_schema`,
  `max_acu_limit`, `attachment_urls` — use v3 create when those matter.

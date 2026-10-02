from functools import cached_property

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str = ""
    devin_api_key: str = ""
    devin_org_id: str = ""
    devin_base_url: str = "https://api.devin.ai/v3"

    # Comma-separated Discord snowflakes; empty = deny everyone.
    allowed_user_ids: str = ""
    hub_channel_id: int | None = None
    command_guild_id: int | None = None

    # Devin session defaults
    devin_mode: str = "lite"
    default_model: str | None = None  # bridge model alias/value; None = server default
    max_acu_limit: int | None = 25
    create_as_user_id: str | None = None

    db_path: str = "devinmobile.db"
    poll_interval_seconds: float = 5.0
    # quiet-streak (min) before the relay posts a "still working?" note; 0=off
    silence_alert_minutes: float = 20.0
    # voice-note steering: audio attachments in bound threads get transcribed
    openai_api_key: str = ""

    # ACP bridge (model-selected cloud sessions). Written by `devin auth login`.
    devin_credentials_path: str = "~/.local/share/devin/credentials.toml"
    devin_api_url_override: str | None = None  # e.g. for staging/enterprise hosts
    bridge_timeout: float = 60.0
    # stream session/update progress into threads via a persistent bridge WS
    acp_progress: bool = True
    # an errored session auto-respawns once as a seeded continuation
    auto_respawn: bool = True

    # GitHub App — powers PR cards/actions, CI status, issue intake, webhooks.
    github_app_id: str = ""
    github_app_private_key_path: str = ""  # PEM file, not inline
    github_app_installation_id: str = ""
    github_webhook_secret: str = ""  # empty = webhook receiver off
    github_webhook_port: int = 8977
    github_merge_method: str = "squash"  # merge | squash | rebase
    github_trigger_label: str = "devin"  # labeling an issue spawns a session
    github_review_label: str = "devin-review"  # labeling a PR spawns a review

    # HTTP task intake — POST /task on the webhook port, bearer-authed.
    # Empty = route off. Same trust model as the webhook secret.
    task_intake_token: str = ""
    # Per-client tokens: "abc:alby,def:sam" → token→name map. A request
    # authenticated by a mapped token attributes the session to that name
    # (spawned_by), ignoring any `by:` field in the payload — the mapping
    # is the stronger claim.
    task_intake_tokens: str = ""

    # Discord-id → Devin-user-id ("123456:devin-u-alby,…") — a session a
    # mapped user spawns is created AS their Devin user on the v3 path, so
    # it shows up in their web session list. Bridge sessions can't remap
    # (ACP creates as the CLI-authed user) — spawned_by stays authoritative.
    devin_user_map: str = ""

    # --- Small-team layer — every phase is off until its var is set --------
    # Access = ALLOWED_USER_IDS ∪ this guild role ∪ the runtime-allowed
    # db table. A Discord role id; guild-side convenience for adding a
    # teammate without an env edit + restart. DM contexts still honor the
    # allowlist only (User objects carry no roles) — keep operators in
    # ALLOWED_USER_IDS regardless.
    required_role_id: int | None = None
    # github-login → discord-id ("alby:123456,…") — teammates who apply the
    # trigger/review labels get their own spawned_by instead of "github"
    github_user_map: str = ""
    # Per-user ACU quota per rolling 24h, session-start attribution; 0=off.
    # Marker spawned_by values (github/intake/token names) are exempt —
    # team infra isn't a user.
    user_acu_daily: float = 0
    # Destructive ops on someone else's resource (/kill, /unschedule,
    # /unnote, ⏸️) need owner-or-admin. Empty → flat trust (solo default).
    admin_user_ids: str = ""
    # discord-id:channel-id lanes — a mapped (allowlisted) spawner's
    # sessions open their thread in their own channel instead of the hub.
    hub_channel_map: str = ""

    @staticmethod
    def _parse_map(raw: str) -> dict[str, str]:
        out = {}
        for pair in raw.split(","):
            k, sep, v = pair.partition(":")
            if sep and k.strip() and v.strip():
                out[k.strip()] = v.strip()
        return out

    @cached_property
    def task_intake_token_map(self) -> dict[str, str]:
        return self._parse_map(self.task_intake_tokens)

    @cached_property
    def devin_user_id_map(self) -> dict[str, str]:
        return self._parse_map(self.devin_user_map)

    @cached_property
    def github_user_id_map(self) -> dict[str, str]:
        return self._parse_map(self.github_user_map)

    @cached_property
    def hub_channel_id_map(self) -> dict[int, int]:
        return {
            int(k): int(v)
            for k, v in self._parse_map(self.hub_channel_map).items()
            if k.isdigit() and v.isdigit()
        }

    @cached_property
    def admin_user_id_set(self) -> frozenset[int]:
        return frozenset(
            int(x) for x in self.admin_user_ids.split(",") if x.strip()
        )

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_user_id_set

    def is_operator(self, user_id: int, role_ids=()) -> bool:
        """env-set ∪ role. The runtime db table joins at the call-site
        layer (bot._is_operator) — Settings stays sync + db-free."""
        if user_id in self.allowed_user_id_set:
            return True
        return bool(
            self.required_role_id and self.required_role_id in role_ids
        )

    @cached_property
    def github_enabled(self) -> bool:
        return bool(
            self.github_app_id
            and self.github_app_private_key_path
            and self.github_app_installation_id
        )

    @cached_property
    def allowed_user_id_set(self) -> frozenset[int]:
        return frozenset(int(x) for x in self.allowed_user_ids.split(",") if x.strip())

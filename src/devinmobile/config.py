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
    poll_interval_seconds: float = 15.0

    # ACP bridge (model-selected cloud sessions). Written by `devin auth login`.
    devin_credentials_path: str = "~/.local/share/devin/credentials.toml"
    devin_api_url_override: str | None = None  # e.g. for staging/enterprise hosts
    bridge_timeout: float = 60.0

    # GitHub App — powers PR cards/actions, CI status, issue intake, webhooks.
    github_app_id: str = ""
    github_app_private_key_path: str = ""  # PEM file, not inline
    github_app_installation_id: str = ""
    github_webhook_secret: str = ""  # empty = webhook receiver off
    github_webhook_port: int = 8977
    github_merge_method: str = "squash"  # merge | squash | rebase

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

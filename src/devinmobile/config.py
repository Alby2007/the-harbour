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
    max_acu_limit: int | None = 25
    create_as_user_id: str | None = None

    db_path: str = "devinmobile.db"
    poll_interval_seconds: float = 15.0

    @cached_property
    def allowed_user_id_set(self) -> frozenset[int]:
        return frozenset(int(x) for x in self.allowed_user_ids.split(",") if x.strip())

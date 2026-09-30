from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class PullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pr_url: str
    pr_state: str | None = None


class Session(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str
    url: str
    status: str  # new|claimed|running|exit|error|suspended|resuming
    status_detail: str | None = None
    title: str | None = None
    tags: list[str] = []
    org_id: str = ""
    created_at: int = 0
    updated_at: int = 0
    acus_consumed: float = 0.0
    pull_requests: list[PullRequest] = []
    structured_output: dict[str, Any] | None = None
    devin_mode: str | None = None


class SessionMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: str
    source: Literal["devin", "user"]
    message: str
    created_at: int
    origin: str | None = None
    user_id: str | None = None
    username: str | None = None


class MessagePage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[SessionMessage]
    end_cursor: str | None = None
    has_next_page: bool = False

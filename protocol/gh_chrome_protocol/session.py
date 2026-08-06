from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class Speed(StrEnum):
    INSTANT = "instant"
    FAST = "fast"
    NORMAL = "normal"
    SLOW = "slow"


class Topic(StrEnum):
    TABS = "tabs"
    DOWNLOADS = "downloads"


class SessionStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    CLOSED = "closed"
    DEAD = "dead"


class CloseReason(StrEnum):
    CLOSED = "closed"
    DEAD = "dead"
    TIMEOUT = "timeout"


class SessionParams(BaseModel):
    width: int = Field(default=1920, ge=320, le=3840)
    height: int = Field(default=1080, ge=240, le=2160)
    fps: int = Field(default=15, ge=1, le=60)
    bitrate: str = "2M"
    mouse_speed: Speed = Speed.NORMAL
    type_speed: Speed = Speed.NORMAL
    scroll_speed: Speed = Speed.NORMAL
    timeout: float = Field(default=30.0, gt=0)
    subscribe: list[Topic] = Field(default_factory=list)


class SessionCreate(BaseModel):
    profile: str | None = None
    persist: bool = True
    max_parallel: int | None = Field(default=None, ge=1)
    params: SessionParams = Field(default_factory=SessionParams)


class SessionState(BaseModel):
    id: UUID
    status: SessionStatus
    state_stale: bool
    profile: str | None
    persist: bool
    params: SessionParams
    last_seq: int


class RunnerConfig(BaseModel):
    session_id: UUID
    params: SessionParams
    profile: str | None
    persist: bool
    has_profile_archive: bool
    segment_seconds: float = 1.0


class ProfileInfo(BaseModel):
    name: str
    size: int | None
    stale: bool
    updated_at: str | None

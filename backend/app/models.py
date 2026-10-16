from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, Session, SQLModel, create_engine

from app.config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    display_name: str
    password_hash: str
    created_at: datetime = Field(default_factory=utcnow)


class Channel(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    topic: str = ""
    band_room_id: str | None = Field(default=None, index=True)
    created_by: int | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)


class ChannelMember(SQLModel, table=True):
    channel_id: int = Field(foreign_key="channel.id", primary_key=True)
    member_type: str = Field(primary_key=True)  # "human" | "agent"
    member_id: int = Field(primary_key=True)


class AgentProfile(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    handle: str = ""
    role: str = "custom"
    instructions: str = ""
    model: str = settings.default_model
    band_agent_id: str = Field(index=True, unique=True)
    band_api_key_enc: str
    is_master: bool = False
    created_by: int | None = Field(default=None, foreign_key="user.id")
    created_by_agent: int | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Message(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    author_type: str  # "human" | "agent" | "system" | "external"
    author_id: int | None = None
    author_name: str
    content: str
    mentions: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    to_humans: bool = True
    band_message_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class AgentTask(SQLModel, table=True):
    id: str = Field(primary_key=True)
    source_key: str = Field(unique=True, index=True)
    channel_id: int = Field(index=True)
    agent_id: int = Field(index=True)
    room_id: str
    prompt: str
    context: str = ""
    reply_to: str = ""
    status: str = Field(default="queued", index=True)
    progress: str = "Waiting for worker"
    result: str = ""
    artifacts: list[dict[str, str]] = Field(default_factory=list, sa_column=Column(JSON))
    error: str | None = None
    attempts: int = 0
    cancel_requested: bool = False
    delivery_status: str = "pending"
    reply_message_id: int | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow, index=True)


class AgentMemory(SQLModel, table=True):
    agent_id: int = Field(primary_key=True)
    channel_id: int = Field(primary_key=True)
    key: str = Field(primary_key=True)
    content: str
    source_task_id: str
    updated_at: datetime = Field(default_factory=utcnow)


class TaskSandbox(SQLModel, table=True):
    task_id: str = Field(primary_key=True)
    name: str = Field(unique=True)
    sandbox_id: str | None = None
    status: str = "provisioning"
    latest_image: str = ""
    frame_id: str = ""
    width: int = 0
    height: int = 0
    last_action: str = ""
    error: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)


class TaskHandoff(SQLModel, table=True):
    """Links a task to the teammate hand-off chain that created it, without new AgentTask columns."""
    task_id: str = Field(primary_key=True)
    root_task_id: str = Field(index=True)
    parent_task_id: str
    from_agent_id: int
    kind: str  # "handoff": a teammate's assigned task; "report": the follow-up returned to the requester
    request: str = ""
    report_back: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class TaskApproval(SQLModel, table=True):
    """An agent action paused for a human decision. The id is the LangGraph interrupt id."""
    id: str = Field(primary_key=True)
    task_id: str = Field(index=True)
    channel_id: int = Field(index=True)
    agent_id: int
    action: str
    details: str = ""
    status: str = Field(default="pending", index=True)  # pending | approved | denied
    decided_by: str = ""
    note: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None


class Schedule(SQLModel, table=True):
    """A recurring or one-off request that the worker turns into an agent task when due."""
    id: int | None = Field(default=None, primary_key=True)
    source_key: str = Field(unique=True, index=True)  # creating task + tool call, for replay-safe creation
    channel_id: int = Field(index=True)
    agent_id: int = Field(index=True)
    room_id: str
    reply_to: str = ""
    prompt: str
    cadence: str  # once | daily | weekdays | weekly | interval
    time_of_day: str = ""  # HH:MM in `timezone`
    weekday: int | None = None  # 0 = Monday, for weekly
    run_date: str = ""  # YYYY-MM-DD, for once
    interval_minutes: int = 0
    timezone: str = "UTC"
    next_run_at: datetime | None = Field(default=None, index=True)
    enabled: bool = Field(default=True, index=True)
    created_by: str = ""
    last_run_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def db() -> Session:
    return Session(engine, expire_on_commit=False)


def get_session():
    with db() as session:
        yield session

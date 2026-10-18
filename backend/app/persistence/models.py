from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Column, UniqueConstraint
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


class DirectMessage(SQLModel, table=True):
    """Marks a channel as one person's private conversation with one agent."""
    channel_id: int = Field(foreign_key="channel.id", primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    # Cleared when the agent is deleted: SQLite can reuse its row id for a future agent.
    agent_id: int | None = Field(default=None, index=True)


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


class MessageDraft(SQLModel, table=True):
    """The reply an agent is still writing; removed when the task's final message is committed."""
    task_id: str = Field(primary_key=True)
    channel_id: int = Field(index=True)
    agent_id: int
    content: str
    updated_at: datetime = Field(default_factory=utcnow)


class MessageComponent(SQLModel, table=True):
    """A table, chart, or spec sheet an agent attached to its reply; linked when the reply is committed."""
    __table_args__ = (UniqueConstraint("task_id", "tool_call_id"),)
    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(index=True)
    tool_call_id: str
    # Keyed by task, not band_message_id: delivery replaces that with the Band message id.
    message_id: int | None = Field(default=None, index=True)
    kind: str  # "table" | "chart" | "spec"
    props: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


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


class TeammateRequest(SQLModel, table=True):
    """An agent asked for a new visible teammate; only the API process can register it on Band."""
    id: int | None = Field(default=None, primary_key=True)
    source_key: str = Field(unique=True, index=True)  # requesting task + tool call, for replay-safe creation
    task_id: str
    channel_id: int
    requested_by_agent_id: int
    name: str
    role: str
    instructions: str = ""
    model: str
    status: str = Field(default="pending", index=True)  # pending | creating | created | failed
    agent_id: int | None = None
    error: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class OutsideAgent(SQLModel, table=True):
    """A Band agent this app doesn't run (e.g. Claude Code or Codex) that people invited into a channel."""
    channel_id: int = Field(foreign_key="channel.id", primary_key=True)
    band_id: str = Field(primary_key=True)
    handle: str
    name: str
    description: str = ""
    added_by: int | None = None
    created_at: datetime = Field(default_factory=utcnow)


class OutsideHandoff(SQLModel, table=True):
    """A request an agent posted to an outside agent through Band; sent at most once."""
    source_key: str = Field(primary_key=True)  # requesting task + tool call
    task_id: str = Field(index=True)
    channel_id: int
    band_id: str
    request: str
    status: str = "sending"  # sending | sent | unknown
    band_message_id: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class ComputerControl(SQLModel, table=True):
    """A person has taken over an agent's computer; agent input actions wait until it ends."""
    key: str = Field(primary_key=True)  # computer key, see computer.computer_key
    user_id: int
    user_name: str
    until: datetime


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


class WorkspaceRole(SQLModel, table=True):
    """A person's workspace role; people without a row are members. The oldest account becomes the owner."""
    user_id: int = Field(foreign_key="user.id", primary_key=True)
    role: str  # owner | admin
    updated_at: datetime = Field(default_factory=utcnow)


class ChannelPolicy(SQLModel, table=True):
    """Who may act for a channel's agents; channels without a row let any member act."""
    channel_id: int = Field(foreign_key="channel.id", primary_key=True)
    approvers: str = "members"  # members | admins
    computer_control: str = "members"  # members | admins: who may take over an agent's computer
    updated_by: int | None = None
    updated_at: datetime = Field(default_factory=utcnow)


class AuditEvent(SQLModel, table=True):
    """One thing a person or agent did, in order. Details are short summaries and never hold secrets."""
    __table_args__ = (UniqueConstraint("task_id", "ref"),)
    id: int | None = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=utcnow, index=True)
    channel_id: int | None = Field(default=None, index=True)  # None for workspace-wide events
    task_id: str | None = Field(default=None, index=True)
    actor_type: str  # human | agent | system
    actor_id: int | None = None
    actor_name: str
    action: str
    detail: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    ref: str | None = None  # e.g. the tool call id, so a replayed call is recorded once per task


class TaskUsage(SQLModel, table=True):
    """Tokens one model call used; keyed by message id so replayed or re-streamed messages count once."""
    __table_args__ = (UniqueConstraint("task_id", "message_id"),)
    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(index=True)
    channel_id: int = Field(index=True)
    agent_id: int = Field(index=True)
    message_id: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    at: datetime = Field(default_factory=utcnow, index=True)


class Budget(SQLModel, table=True):
    """A daily token limit (UTC day) for one agent or one channel."""
    scope: str = Field(primary_key=True)  # agent | channel
    scope_id: int = Field(primary_key=True)
    daily_tokens: int
    updated_by: int | None = None
    updated_at: datetime = Field(default_factory=utcnow)


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def db() -> Session:
    return Session(engine, expire_on_commit=False)


def dm_owner(channel_id: int) -> int | None:
    """The user who owns this direct-message channel, or None for a shared channel."""
    with db() as session:
        dm = session.get(DirectMessage, channel_id)
        return dm.user_id if dm else None


def get_session():
    with db() as session:
        yield session

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


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def db() -> Session:
    return Session(engine, expire_on_commit=False)


def get_session():
    with db() as session:
        yield session

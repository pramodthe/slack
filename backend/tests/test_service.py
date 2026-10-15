from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlmodel import select

import app.service as service_module
from app.agent_runner import BandIdentity, current_room
from app.auth import encrypt_secret
from app.band_org import BandOrg, Owner
from app.models import AgentProfile, Channel, ChannelMember, Message, User, db, init_db
from app.service import AppService, Create_AgentInput, List_AgentsInput


class FakeOrg(BandOrg):
    def __init__(self) -> None:
        super().__init__()
        self.owner = Owner(id="user-owner", name="Pat Owner", handle="pat")
        self._rest = object()
        self.rooms: list[str] = []
        self.participants: dict[str, set[str]] = {}
        self.registered: list[str] = []
        self.inbox: list[SimpleNamespace] = []

    async def create_room(self, title: str) -> str:
        room_id = f"room-{len(self.rooms) + 1}"
        self.rooms.append(title)
        self.participants[room_id] = {self.owner.id}
        return room_id

    async def add_participant(self, room_id: str, band_id: str) -> None:
        self.participants.setdefault(room_id, set()).add(band_id)

    async def register_agent(self, name: str, description: str) -> tuple[str, str]:
        self.registered.append(name)
        return f"agent-{name.lower()}", f"key-{name.lower()}"

    async def messages_since(self, room_id: str, since):
        return list(self.inbox)


@pytest.fixture
def svc(monkeypatch) -> AppService:
    init_db()
    with db() as session:
        for model in (Message, ChannelMember, AgentProfile, Channel, User):
            for row in session.exec(select(model)).all():
                session.delete(row)
        session.commit()

    async def identity(api_key: str) -> BandIdentity:
        name = api_key.removeprefix("key-")
        return BandIdentity(id=f"agent-{name}", name=name.title(), handle=f"pat/{name}")

    monkeypatch.setattr(service_module, "fetch_identity", identity)
    service = AppService()
    service.org = FakeOrg()
    return service


def _messages(channel_id: int) -> list[Message]:
    with db() as session:
        return session.exec(select(Message).where(Message.channel_id == channel_id).order_by(Message.id)).all()


def _user() -> User:
    with db() as session:
        user = User(username="alice", display_name="Alice", password_hash="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def test_channels_are_band_rooms_and_the_master_creates_agents(svc: AppService):
    async def scenario():
        await svc._ensure_master()
        channel = await svc.create_channel("launch", "", _user())
        room_id = channel["band_room_id"]
        master = svc.master()

        assert svc.org.rooms == ["#launch"]
        assert master.band_agent_id in svc.org.participants[room_id]
        assert channel["agent_ids"] == [master.id]

        create_tool, list_tool = (handler for _, handler in svc._master_tools(master))
        token = current_room.set(room_id)
        try:
            created = await create_tool(Create_AgentInput(name="Scout", role="researcher", instructions="Find facts"))
            listed = await list_tool(List_AgentsInput())
        finally:
            current_room.reset(token)
        return channel["id"], room_id, master, created, listed

    channel_id, room_id, master, created, listed = asyncio.run(scenario())

    assert created == {
        "name": "Scout",
        "handle": "pat/scout",
        "id": "agent-scout",
        "role": "researcher",
        "in_this_room": True,
    }
    assert svc.org.registered == ["Master", "Scout"]
    assert "agent-scout" in svc.org.participants[room_id]
    assert [(a["name"], a["in_this_room"]) for a in listed] == [("Scout", True)]
    with db() as session:
        scout = session.exec(select(AgentProfile).where(AgentProfile.name == "Scout")).one()
        assert scout.created_by_agent == master.id
        assert session.get(ChannelMember, (channel_id, "agent", scout.id)) is not None
    assert _messages(channel_id)[-1].content == "Master created **Scout** (Researcher)."


def test_agent_replies_to_the_owner_reach_humans_and_polling_dedupes(svc: AppService):
    with db() as session:
        channel = Channel(name="svc", band_room_id="room-svc")
        writer = AgentProfile(name="Writer", handle="pat/writer", band_agent_id="agent-writer", band_api_key_enc=encrypt_secret("k"))
        session.add_all([channel, writer])
        session.commit()
        channel_id, writer_id = channel.id, writer.id

    asyncio.run(svc.agent_message(writer_id, "room-svc", "@pat Here is the draft, Alice.", ["pat"], "m1"))
    asyncio.run(svc.agent_message(writer_id, "room-unknown", "lost", ["pat"], "m2"))

    now = datetime.now(timezone.utc)

    def item(i: int, sender_id: str, content: str, name: str | None = None):
        return SimpleNamespace(
            id=f"m{i}", sender_id=sender_id, sender_name=name, content=content, inserted_at=now + timedelta(seconds=i)
        )

    svc.org.inbox = [
        item(1, "agent-writer", "@pat Here is the draft, Alice."),
        item(3, "user-owner", "**Alice:** hi"),
        item(4, "user-guest", "@pat hello from the Band app", "Guest"),
        item(5, "agent-writer", "@pat fallback reply"),
    ]
    asyncio.run(svc.sync_room(channel_id, "room-svc", now))

    stored = _messages(channel_id)
    assert [(m.author_type, m.author_name, m.content, m.to_humans) for m in stored] == [
        ("agent", "Writer", "Here is the draft, Alice.", True),
        ("external", "Guest", "hello from the Band app", True),
        ("agent", "Writer", "fallback reply", True),
    ]
    with db() as session:
        assert session.get(ChannelMember, (channel_id, "agent", writer_id)) is not None

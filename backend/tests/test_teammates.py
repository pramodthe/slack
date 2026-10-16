from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlmodel import select

import app.tasks.teammates as teammates_module
from app.auth import encrypt_secret, issue_token
from app.config import settings
from app.main import app
from app.persistence.models import (AgentProfile, AgentTask, Channel, ChannelMember, DirectMessage, Message,
                                    OutsideAgent, OutsideHandoff, TeammateRequest, User, db)
from app.runtime.harness import build_harness
from app.runtime.tools import tools_for
from app.tasks import enqueue, update_task
from app.tasks.teammates import create_teammate, outside_reply
from app.tasks.worker import execute
from app.workspace.service import AppService, ServiceError
from test_service import FakeOrg, svc  # noqa: F401  (svc is a fixture)
from test_tasks import ScriptedModel

PEERS = [
    SimpleNamespace(id="peer-tom", handle="pat/tom", name="Tom", description="Claude Code on Pat's laptop",
                    online=True, is_external=False),
    SimpleNamespace(id="agent-master", handle="pat/master", name="Master", description="", online=True,
                    is_external=False),
]


class PeerOrg(FakeOrg):
    async def agent_peers(self, room_id: str) -> list:
        return [p for p in PEERS if p.id not in self.participants.get(room_id, set())]

    async def remove_participant(self, room_id: str, band_id: str) -> None:
        self.participants.get(room_id, set()).discard(band_id)


@pytest.fixture
def team(svc: AppService):  # noqa: F811
    with db() as session:
        for model in (TeammateRequest, OutsideAgent, OutsideHandoff):
            for row in session.exec(select(model)).all():
                session.delete(row)
        session.commit()
    svc.org = PeerOrg()
    asyncio.run(svc._ensure_master())
    master = svc.master()
    with db() as session:
        channel = Channel(name="crew", band_room_id="room-crew")
        user = User(username="alice", display_name="Alice", password_hash="x")
        session.add_all([channel, user])
        session.commit()
        session.refresh(channel)
        session.refresh(user)
        session.add_all([ChannelMember(channel_id=channel.id, member_type="human", member_id=user.id),
                         ChannelMember(channel_id=channel.id, member_type="agent", member_id=master.id)])
        session.commit()
    task = enqueue(agent_id=master.id, channel_id=channel.id, room_id="room-crew", source_id="kickoff",
                   prompt="Build a small team")
    return svc, task, master, channel, user


async def _with_watcher(svc: AppService, work):
    watcher = asyncio.create_task(svc._watch_teammate_requests())
    try:
        return await work
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


def test_agent_created_teammate_joins_channel_once(team):
    svc, task, master, channel, _ = team

    async def scenario():
        first = await _with_watcher(svc, create_teammate(
            task, name="Scout", role="Researcher", instructions="Find primary sources", call_id="call-1", wait=5))
        assert first["status"] == "created" and first["teammate"] == "Scout"
        replay = await create_teammate(task, name="Scout", role="researcher", instructions="", call_id="call-1", wait=1)
        assert replay["status"] == "created"
        other = await create_teammate(task, name="scout", role="writer", instructions="", call_id="call-2", wait=1)
        assert other["status"] == "exists"

    asyncio.run(scenario())
    assert svc.org.registered == ["Master", "Scout"]
    with db() as session:
        scout = session.exec(select(AgentProfile).where(AgentProfile.name == "Scout")).one()
        assert scout.role == "researcher" and scout.created_by_agent == master.id and scout.model == master.model
        assert scout.instructions == "Find primary sources"
        assert session.get(ChannelMember, (channel.id, "agent", scout.id))
        notes = session.exec(select(Message).where(Message.channel_id == channel.id,
                                                   Message.author_type == "system")).all()
        assert [n.content for n in notes] == [
            f"{master.name} created **Scout** (Researcher) as a teammate in this channel."]
    assert scout.band_agent_id in svc.org.participants["room-crew"]


def test_teammate_requests_are_validated_and_limited(team, monkeypatch):
    svc, task, _, channel, user = team

    async def scenario():
        assert "Unknown role" in (await create_teammate(task, name="Scout", role="pilot", instructions="",
                                                        call_id="a", wait=0))["error"]
        assert "3-40" in (await create_teammate(task, name="X", role="writer", instructions="",
                                                call_id="b", wait=0))["error"]
        monkeypatch.setattr(teammates_module, "settings", dataclasses.replace(settings, max_agent_teammates=1))
        made = await _with_watcher(svc, create_teammate(task, name="Quill", role="writer", instructions="",
                                                         call_id="c", wait=5))
        assert made["status"] == "created"
        over = await create_teammate(task, name="Pixel", role="coder", instructions="", call_id="d", wait=0)
        assert "limit" in over["error"]
        with db() as session:
            session.add(DirectMessage(channel_id=channel.id, user_id=user.id, agent_id=task.agent_id))
            session.commit()
        assert "one-on-one" in (await create_teammate(task, name="Pixel", role="coder", instructions="",
                                                      call_id="e", wait=0))["error"]

    asyncio.run(scenario())
    assert svc.org.registered == ["Master", "Quill"]


def test_unclaimed_request_expires_and_is_never_created(team):
    svc, task, _, _, _ = team
    result = asyncio.run(create_teammate(task, name="Scout", role="researcher", instructions="", call_id="slow",
                                         wait=0.6))
    assert "didn't respond" in result["error"]
    with db() as session:
        request = session.exec(select(TeammateRequest).where(TeammateRequest.source_key == f"{task.id}:slow")).one()
        assert request.status == "failed"
    asyncio.run(svc.process_teammate_request(request.id))
    assert svc.org.registered == ["Master"]


def test_band_offline_fails_the_request_with_a_clear_error(team):
    svc, task, _, _, _ = team
    svc.org._rest = None
    result = asyncio.run(_with_watcher(svc, create_teammate(task, name="Scout", role="researcher", instructions="",
                                                            call_id="offline", wait=5)))
    assert "Not connected to Band" in result["error"]
    with db() as session:
        assert not session.exec(select(AgentProfile).where(AgentProfile.name == "Scout")).first()


def test_master_creates_a_teammate_then_hands_off_to_it(tmp_path, team):
    svc, task, master, _, _ = team
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "create_teammate", "id": "make-1", "args": {
            "name": "Quill", "role": "writer", "instructions": "Short, punchy copy"}}]),
        AIMessage(content="", tool_calls=[{"name": "ask_teammate", "id": "ask-1", "args": {
            "teammate": "Quill", "request": "Write a tagline."}}]),
        AIMessage(content="Quill is on the tagline."),
    ])

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "recruit.db")) as saver:
            await saver.setup()
            update_task(task.id, status="running")
            return await execute(task, saver, factory=lambda t, p, c, **kw: build_harness(t, p, c, model=model, **kw))

    assert asyncio.run(_with_watcher(svc, scenario())) == "Quill is on the tagline."
    with db() as session:
        quill = session.exec(select(AgentProfile).where(AgentProfile.name == "Quill")).one()
        child = session.exec(select(AgentTask).where(AgentTask.agent_id == quill.id)).one()
        assert child.prompt == f"**{master.name}:** Write a tagline."


def test_only_master_can_create_teammates(team):
    _, task, _, _, _ = team
    assert "create_teammate" in {t.name for t in tools_for(task)}
    with db() as session:
        writer = AgentProfile(name="Writer", role="writer", band_agent_id="agent-writer",
                              band_api_key_enc=encrypt_secret("k"))
        session.add(writer)
        session.commit()
        session.refresh(writer)
    other = enqueue(agent_id=writer.id, channel_id=task.channel_id, room_id=task.room_id, source_id="w",
                    prompt="Hi")
    assert "create_teammate" not in {t.name for t in tools_for(other)}


def test_outside_agents_can_be_invited_listed_and_removed(team):
    svc, _, _, channel, user = team

    async def scenario():
        peers = await svc.list_peers(channel.id)
        assert [p["band_id"] for p in peers] == ["peer-tom"]
        await svc.invite_outside_agent(user, channel.id, "peer-tom")
        await svc.invite_outside_agent(user, channel.id, "peer-tom")
        assert await svc.list_peers(channel.id) == []
        with pytest.raises(ServiceError, match="belongs to this workspace"):
            await svc.invite_outside_agent(user, channel.id, "agent-master")
        with db() as session:
            listed = svc.serialize_channel(session, session.get(Channel, channel.id))["outside_agents"]
        assert listed == [{"band_id": "peer-tom", "handle": "pat/tom", "name": "Tom",
                           "description": "Claude Code on Pat's laptop"}]
        assert "peer-tom" in svc.org.participants["room-crew"]
        await svc.remove_outside_agent(channel.id, "peer-tom")
        assert "peer-tom" not in svc.org.participants["room-crew"]

    asyncio.run(scenario())
    notes = [m.content for m in _system(channel.id)]
    assert notes == ["Alice invited the outside Band agent **Tom** (@pat/tom).", "**Tom** was removed from the channel."]


def _system(channel_id: int) -> list[Message]:
    with db() as session:
        return session.exec(select(Message).where(Message.channel_id == channel_id,
                                                  Message.author_type == "system").order_by(Message.id)).all()


class FakeRest:
    sent: list = []
    fail = False

    def __init__(self, api_key: str, base_url: str) -> None:
        self.agent_api_messages = self

    async def create_agent_chat_message(self, room_id, message, request_options):
        FakeRest.sent.append((room_id, message))
        if FakeRest.fail:
            raise TimeoutError("network")
        return SimpleNamespace(data=SimpleNamespace(id=f"band-msg-{len(FakeRest.sent)}"))


async def _no_close(client) -> None:
    return None


def _ask(task, teammate: str, call_id: str) -> dict:
    tool = {t.name: t for t in tools_for(task)}["ask_teammate"]
    message = asyncio.run(tool.ainvoke({"type": "tool_call", "name": "ask_teammate", "id": call_id, "args": {
        "teammate": teammate, "request": "Run the benchmark and report the p95."}}))
    return json.loads(message.content)


def test_outside_handoff_is_sent_once_through_band(team, monkeypatch):
    _, task, master, channel, _ = team
    monkeypatch.setattr(teammates_module, "AsyncRestClient", FakeRest)
    monkeypatch.setattr(teammates_module, "aclose_rest_client", _no_close)
    FakeRest.sent, FakeRest.fail = [], False
    with db() as session:
        session.add(OutsideAgent(channel_id=channel.id, band_id="peer-tom", handle="pat/tom", name="Tom"))
        session.commit()

    first = _ask(task, "@tom", "out-1")
    assert first["status"] == "handed_off" and first["outside"]
    assert _ask(task, "Tom", "out-1") == first
    assert len(FakeRest.sent) == 1
    room_id, message = FakeRest.sent[0]
    assert room_id == "room-crew" and [m.id for m in message.mentions] == ["peer-tom"]
    assert f"mention @{master.name}" in message.content
    with db() as session:
        handoff = session.get(OutsideHandoff, f"{task.id}:out-1")
        assert handoff.status == "sent" and handoff.band_message_id == "band-msg-1"
        local = session.exec(select(Message).where(Message.band_message_id == "handoff:out-1")).one()
        assert local.author_id == master.id and not local.to_humans
        assert local.mentions == [{"type": "external", "id": None, "name": "Tom"}]

    FakeRest.fail = True
    failed = _ask(task, "Tom", "out-2")
    assert "Don't resend" in failed["error"]
    assert "Don't resend" in _ask(task, "Tom", "out-2")["error"]
    assert len(FakeRest.sent) == 2


def test_outside_reply_comes_back_as_the_answer_to_the_request(team, monkeypatch):
    svc, task, master, channel, _ = team
    monkeypatch.setattr(teammates_module, "AsyncRestClient", FakeRest)
    monkeypatch.setattr(teammates_module, "aclose_rest_client", _no_close)
    FakeRest.sent, FakeRest.fail = [], False
    with db() as session:
        session.add(OutsideAgent(channel_id=channel.id, band_id="peer-tom", handle="pat/tom", name="Tom"))
        session.commit()
    _ask(task, "Tom", "out-1")

    now = datetime.now(timezone.utc)
    svc.org.inbox = [SimpleNamespace(id="tom-1", sender_id="peer-tom", sender_name="Tom", inserted_at=now,
                                     content=f"@[[{master.band_agent_id}]] p95 is 120 ms.")]
    asyncio.run(svc.sync_room(channel.id, "room-crew", now - timedelta(seconds=1)))
    with db() as session:
        stored = session.exec(select(Message).where(Message.band_message_id == "tom-1")).one()
        assert stored.author_type == "external" and stored.content == f"@{master.name} p95 is 120 ms."
        prompt = outside_reply(session, master.id, channel.id, "peer-tom", stored.content)
        assert "Run the benchmark and report the p95." in prompt and task.prompt in prompt
        assert prompt.index("p95 is 120 ms.") > prompt.index(task.prompt)
        assert outside_reply(session, master.id, channel.id, "someone-else", "hi") == "hi"
        assert outside_reply(session, master.id + 999, channel.id, "peer-tom", "hi") == "hi"


def test_outside_agent_endpoints_reject_dms_and_offline_band(team):
    _, task, _, channel, user = team
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as client:
        assert client.get(f"/api/channels/{channel.id}/peers", headers=headers).status_code == 503
        with db() as session:
            session.add(DirectMessage(channel_id=channel.id, user_id=user.id, agent_id=task.agent_id))
            session.commit()
        response = client.post(f"/api/channels/{channel.id}/outside-agents", headers=headers,
                               json={"band_id": "peer-tom"})
        assert response.status_code == 409

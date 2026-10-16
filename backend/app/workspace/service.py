from __future__ import annotations

import asyncio
import logging
import re
from uuid import uuid4
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update
from sqlmodel import Session, select

from app.band.listener import AgentManager, describe_error, fetch_identity
from app.auth import encrypt_secret
from app.band.org import BandOrg
from app.config import settings
from app.workspace.events import bus
from app.persistence.models import (AgentMemory, AgentTask, AgentProfile, Channel, ChannelMember, DirectMessage, Message,
                                    MessageDraft, OutsideAgent, Schedule, TaskSandbox, TeammateRequest, User, db,
                                    dm_owner)
from app.workspace.roles import MASTER_ROLE, ROLES, role_label
from app.tasks import channel_context, display_names

logger = logging.getLogger(__name__)

bus.audience = dm_owner


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def serialize_user(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "online": user.id in bus.online_user_ids,
    }


def serialize_message(message: Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "channel_id": message.channel_id,
        "author_type": message.author_type,
        "author_id": message.author_id,
        "author_name": message.author_name,
        "content": message.content,
        "mentions": message.mentions,
        "to_humans": message.to_humans,
        "created_at": message.created_at.isoformat(),
    }


def serialize_outside_agent(outsider: OutsideAgent) -> dict[str, Any]:
    return {
        "band_id": outsider.band_id,
        "handle": outsider.handle,
        "name": outsider.name,
        "description": outsider.description,
    }


def _mentioned(content: str, *names: str) -> bool:
    return any(
        name and re.search(rf"@{re.escape(name)}(?![\w-])", content, re.IGNORECASE)
        for name in names
    )


class AppService:
    """Keeps the app's channels, agents and messages in step with the org's Band account."""

    def __init__(self) -> None:
        self.org = BandOrg()
        self.agents = AgentManager(self)
        self._room_to_channel: dict[str, int] = {}
        self._poll_since: dict[str, datetime] = {}
        self._poller: asyncio.Task | None = None
        self._task_poller: asyncio.Task | None = None
        self._teammate_poller: asyncio.Task | None = None
        self._dm_lock = asyncio.Lock()

    # --- lifecycle ---

    async def startup(self) -> None:
        with db() as session:
            latest = session.exec(select(Message.id).order_by(Message.id.desc()).limit(1)).first() or 0
        self._task_poller = asyncio.create_task(self._watch_task_messages(latest), name="task-messages")
        self._teammate_poller = asyncio.create_task(self._watch_teammate_requests(), name="teammate-requests")
        await self.org.connect()
        await self._publish_band_status()
        if not self.org.online:
            return
        try:
            await self._ensure_rooms()
            await self._ensure_master()
        except Exception as exc:
            logger.exception("Band setup failed")
            self.org.error = describe_error(exc)
            await self._publish_band_status()
        with db() as session:
            profiles = session.exec(select(AgentProfile)).all()
        for profile in profiles:
            if profile.id not in self.agents.status:
                await self.start_agent(profile)
        self._poller = asyncio.create_task(self._poll_forever(), name="band-poller")

    async def shutdown(self) -> None:
        for poller in (self._task_poller, self._teammate_poller):
            if poller is not None:
                poller.cancel()
                await asyncio.gather(poller, return_exceptions=True)
        if self._poller is not None:
            self._poller.cancel()
            await asyncio.gather(self._poller, return_exceptions=True)
        await self.agents.stop_all()
        await self.org.close()

    async def _watch_task_messages(self, after_id: int) -> None:
        """Bridge worker commits into WebSocket updates: replies, hand-offs, approval requests, and task progress."""
        active: dict[str, dict[str, Any]] = {}
        drafts: dict[str, dict[str, Any]] = {}
        while True:
            await asyncio.sleep(0.5)
            try:
                with db() as session:
                    # Read drafts first: a reply committed in between then shows up as a message, never as a gap.
                    written = {d.task_id: {"task_id": d.task_id, "channel_id": d.channel_id, "agent_id": d.agent_id,
                                           "content": d.content} for d in session.exec(select(MessageDraft)).all()}
                    messages = session.exec(select(Message).join(
                        AgentTask, AgentTask.reply_message_id == Message.id
                    ).where(Message.id > after_id)).all() + session.exec(select(Message).where(
                        Message.id > after_id, Message.band_message_id.startswith("handoff:")
                        | Message.band_message_id.startswith("approval:"))).all()
                    working = session.exec(select(AgentTask).where(
                        AgentTask.status.in_(["queued", "running", "waiting"]))).all()
                # Updates go before replies and removals after, so a draft never reappears or leaves a gap.
                for task_id, draft in written.items():
                    if drafts.get(task_id) != draft:
                        await bus.publish({"type": "draft", **draft})
                for message in sorted(messages, key=lambda m: m.id):
                    await bus.publish({"type": "message", "message": serialize_message(message)})
                    after_id = message.id
                # The worker adds hand-off teammates as channel members; refresh member lists once per channel.
                for channel_id in {m.channel_id for m in messages if (m.band_message_id or "").startswith("handoff:")}:
                    await self.publish_channel(channel_id)
                for task_id in drafts.keys() - written.keys():
                    await bus.publish({"type": "draft", **drafts[task_id], "content": ""})
                drafts = written
                current = {t.id: {"id": t.id, "channel_id": t.channel_id, "agent_id": t.agent_id,
                                  "status": t.status, "progress": t.progress} for t in working}
                for task_id, info in current.items():
                    if active.get(task_id) != info:
                        await bus.publish({"type": "task_activity", "task": info})
                for task_id in active.keys() - current.keys():
                    await bus.publish({"type": "task_activity", "task": {**active[task_id], "status": "done"}})
                active = current
            except Exception:
                logger.exception("Could not publish worker updates")

    async def _watch_teammate_requests(self) -> None:
        """Register teammates that agents asked for; the worker can't, since Band registration lives here."""
        while True:
            await asyncio.sleep(0.5)
            try:
                with db() as session:
                    pending = session.exec(select(TeammateRequest.id).where(
                        TeammateRequest.status == "pending").order_by(TeammateRequest.id)).all()
                for request_id in pending:
                    await self.process_teammate_request(request_id)
            except Exception:
                logger.exception("Could not process teammate requests")

    async def process_teammate_request(self, request_id: int) -> None:
        with db() as session:
            # Claiming first stops the worker expiring a request whose Band registration is under way.
            claimed = session.exec(update(TeammateRequest).where(
                TeammateRequest.id == request_id, TeammateRequest.status == "pending").values(status="creating"))
            session.commit()
            request = session.get(TeammateRequest, request_id)
            requester = session.get(AgentProfile, request.requested_by_agent_id)
        if not claimed.rowcount:
            return
        status, agent_id, error = "failed", None, ""
        try:
            profile = await self.create_agent(name=request.name, role=request.role, instructions=request.instructions,
                                              model=request.model, channel_id=request.channel_id,
                                              created_by_agent=request.requested_by_agent_id)
            status, agent_id = "created", profile.id
        except ServiceError as exc:
            error = exc.message
        except Exception as exc:
            logger.exception("Creating teammate %s failed", request.name)
            error = type(exc).__name__
        with db() as session:
            session.exec(update(TeammateRequest).where(TeammateRequest.id == request_id).values(
                status=status, agent_id=agent_id, error=error[:500]))
            session.commit()
        if status == "created":
            await self.system_message(request.channel_id, f"{requester.name if requester else 'An agent'} created "
                                      f"**{profile.name}** ({role_label(profile.role)}) as a teammate in this channel.")

    def _require_band(self) -> None:
        if not self.org.online:
            raise ServiceError(f"Not connected to Band: {self.org.error or 'starting up'}", 503)

    async def _ensure_rooms(self) -> None:
        with db() as session:
            channels = session.exec(select(Channel).where(Channel.band_room_id == None)).all()  # noqa: E711
        for channel in channels:
            room_id = await self.org.create_room(f"#{channel.name}")
            with db() as session:
                stored = session.get(Channel, channel.id)
                stored.band_room_id = room_id
                session.add(stored)
                session.commit()
            await self.publish_channel(channel.id)

    async def _ensure_master(self) -> None:
        with db() as session:
            master = session.exec(select(AgentProfile).where(AgentProfile.is_master == True)).first()  # noqa: E712
            private = set(session.exec(select(DirectMessage.channel_id)).all())
            channel_ids = [c.id for c in session.exec(select(Channel)).all() if c.id not in private]
        if master is None:
            master = await self._register_agent(
                name=settings.master_agent_name,
                role="master",
                instructions="",
                model=settings.default_model,
                is_master=True,
            )
        for channel_id in channel_ids:
            await self.add_agent_to_channel(channel_id, master)

    def master(self) -> AgentProfile | None:
        with db() as session:
            return session.exec(select(AgentProfile).where(AgentProfile.is_master == True)).first()  # noqa: E712

    # --- serialization ---

    def serialize_agent(self, profile: AgentProfile) -> dict[str, Any]:
        status = self.agents.status.get(profile.id or 0, {"state": "stopped", "error": None})
        return {
            "id": profile.id,
            "name": profile.name,
            "handle": profile.handle,
            "role": profile.role,
            "role_label": role_label(profile.role),
            "instructions": profile.instructions,
            "model": profile.model,
            "band_agent_id": profile.band_agent_id,
            "is_master": profile.is_master,
            "status": status,
            "created_by": profile.created_by,
            "created_by_agent": profile.created_by_agent,
        }

    @staticmethod
    def serialize_channel(session: Session, channel: Channel) -> dict[str, Any]:
        members = session.exec(
            select(ChannelMember).where(ChannelMember.channel_id == channel.id)
        ).all()
        dm = session.get(DirectMessage, channel.id)
        outsiders = session.exec(select(OutsideAgent).where(OutsideAgent.channel_id == channel.id)
                                 .order_by(OutsideAgent.created_at)).all()
        return {
            "id": channel.id,
            "name": channel.name,
            "topic": channel.topic,
            "band_room_id": channel.band_room_id,
            "human_ids": [m.member_id for m in members if m.member_type == "human"],
            "agent_ids": [m.member_id for m in members if m.member_type == "agent"],
            "outside_agents": [serialize_outside_agent(o) for o in outsiders],
            "is_dm": dm is not None,
            "dm_agent_id": dm.agent_id if dm else None,
        }

    async def publish_channel(self, channel_id: int) -> None:
        with db() as session:
            channel = session.get(Channel, channel_id)
            if channel:
                await bus.publish({"type": "channel", "channel": self.serialize_channel(session, channel)})

    async def _publish_band_status(self) -> None:
        await bus.publish({"type": "band_status", "band": self.org.status()})

    # --- channels ---

    def _channel_for_room(self, session: Session, room_id: str) -> Channel | None:
        channel_id = self._room_to_channel.get(room_id)
        if channel_id is not None:
            return session.get(Channel, channel_id)
        channel = session.exec(select(Channel).where(Channel.band_room_id == room_id)).first()
        if channel:
            self._room_to_channel[room_id] = channel.id
        return channel

    async def create_channel(self, name: str, topic: str, creator: User) -> dict[str, Any]:
        self._require_band()
        with db() as session:
            if session.exec(select(Channel).where(Channel.name == name)).first():
                raise ServiceError("A channel with that name already exists", 409)
        try:
            room_id = await self.org.create_room(f"#{name}")
        except Exception as exc:
            raise ServiceError(f"Band couldn't create the room: {describe_error(exc)}", 502) from exc
        with db() as session:
            channel = Channel(name=name, topic=topic, band_room_id=room_id, created_by=creator.id)
            session.add(channel)
            session.commit()
            session.refresh(channel)
            session.add(ChannelMember(channel_id=channel.id, member_type="human", member_id=creator.id))
            session.commit()
            channel_id = channel.id
        self._room_to_channel[room_id] = channel_id
        master = self.master()
        if master:
            await self.add_agent_to_channel(channel_id, master, publish=False)
        await self.publish_channel(channel_id)
        with db() as session:
            return self.serialize_channel(session, session.get(Channel, channel_id))

    async def open_dm(self, user: User, profile: AgentProfile) -> dict[str, Any]:
        """The user's private channel with this agent, created with its own Band room on first use."""
        async with self._dm_lock:
            with db() as session:
                existing = session.exec(select(DirectMessage).where(
                    DirectMessage.user_id == user.id, DirectMessage.agent_id == profile.id)).first()
                if existing:
                    return self.serialize_channel(session, session.get(Channel, existing.channel_id))
            self._require_band()
            try:
                room_id = await self.org.create_room(f"{user.display_name} + {profile.name}")
            except Exception as exc:
                raise ServiceError(f"Band couldn't create the room: {describe_error(exc)}", 502) from exc
            with db() as session:
                channel = Channel(name=f"dm-{uuid4().hex[:12]}", topic=f"Direct messages with {profile.name}",
                                  band_room_id=room_id, created_by=user.id)
                session.add(channel)
                session.flush()
                session.add_all([DirectMessage(channel_id=channel.id, user_id=user.id, agent_id=profile.id),
                                 ChannelMember(channel_id=channel.id, member_type="human", member_id=user.id)])
                session.commit()
                channel_id = channel.id
            self._room_to_channel[room_id] = channel_id
        await self.add_agent_to_channel(channel_id, profile)
        with db() as session:
            return self.serialize_channel(session, session.get(Channel, channel_id))

    async def add_agent_to_channel(self, channel_id: int, profile: AgentProfile, *, publish: bool = True) -> None:
        with db() as session:
            channel = session.get(Channel, channel_id)
            if channel is None:
                raise ServiceError("Channel not found", 404)
            room_id = channel.band_room_id
            if not session.get(ChannelMember, (channel_id, "agent", profile.id)):
                session.add(ChannelMember(channel_id=channel_id, member_type="agent", member_id=profile.id))
                session.commit()
        if room_id and self.org.online:
            try:
                await self.org.add_participant(room_id, profile.band_agent_id)
            except Exception as exc:
                await self.system_message(channel_id, f"Couldn't add {profile.name} to the Band room: {describe_error(exc)}")
        if publish:
            await self.publish_channel(channel_id)

    async def remove_agent_from_channel(self, channel_id: int, profile: AgentProfile) -> None:
        with db() as session:
            member = session.get(ChannelMember, (channel_id, "agent", profile.id))
            channel = session.get(Channel, channel_id)
            room_id = channel.band_room_id if channel else None
            if member:
                session.delete(member)
                session.commit()
        await self.publish_channel(channel_id)
        if room_id and self.org.online:
            try:
                await self.org.remove_participant(room_id, profile.band_agent_id)
            except Exception:
                logger.exception("Couldn't remove %s from room %s", profile.name, room_id)

    async def list_peers(self, channel_id: int) -> list[dict[str, Any]]:
        """Band agents outside this app that could be invited to the channel."""
        self._require_band()
        with db() as session:
            channel = session.get(Channel, channel_id)
            if channel is None or not channel.band_room_id:
                raise ServiceError("Channel not found", 404)
            ours = set(session.exec(select(AgentProfile.band_agent_id)).all())
            invited = set(session.exec(select(OutsideAgent.band_id).where(OutsideAgent.channel_id == channel_id)).all())
        try:
            peers = await self.org.agent_peers(channel.band_room_id)
        except Exception as exc:
            raise ServiceError(f"Band couldn't list agents: {describe_error(exc)}", 502) from exc
        return [{
            "band_id": p.id, "handle": p.handle, "name": p.name, "description": p.description or "",
            "online": bool(p.online), "is_external": bool(p.is_external),
        } for p in peers if p.id not in ours and p.id not in invited]

    async def invite_outside_agent(self, user: User, channel_id: int, band_id: str) -> None:
        self._require_band()
        with db() as session:
            channel = session.get(Channel, channel_id)
            if channel is None or not channel.band_room_id:
                raise ServiceError("Channel not found", 404)
            if session.get(OutsideAgent, (channel_id, band_id)):
                return
            if session.exec(select(AgentProfile.id).where(AgentProfile.band_agent_id == band_id)).first():
                raise ServiceError("That agent belongs to this workspace; add it as a teammate instead", 409)
        peers = await self.list_peers(channel_id)
        peer = next((p for p in peers if p["band_id"] == band_id), None)
        if peer is None:
            raise ServiceError("That Band agent isn't available to invite", 404)
        with db() as session:
            # Agents name teammates in ask_teammate, so a name must point at exactly one of them.
            if session.exec(select(AgentProfile.id).where(AgentProfile.name == peer["name"])).first():
                raise ServiceError(f"A teammate is already named {peer['name']!r}", 409)
        try:
            await self.org.add_participant(channel.band_room_id, band_id)
        except Exception as exc:
            raise ServiceError(f"Band couldn't add the agent: {describe_error(exc)}", 502) from exc
        with db() as session:
            session.add(OutsideAgent(channel_id=channel_id, band_id=band_id, handle=peer["handle"], name=peer["name"],
                                     description=peer["description"], added_by=user.id))
            session.commit()
        await self.system_message(channel_id, f"{user.display_name} invited the outside Band agent "
                                  f"**{peer['name']}** (@{peer['handle']}).")
        await self.publish_channel(channel_id)

    async def remove_outside_agent(self, channel_id: int, band_id: str) -> None:
        with db() as session:
            outsider = session.get(OutsideAgent, (channel_id, band_id))
            if outsider is None:
                raise ServiceError("That agent isn't in this channel", 404)
            channel = session.get(Channel, channel_id)
            name = outsider.name
            session.delete(outsider)
            session.commit()
        if channel.band_room_id and self.org.online:
            try:
                await self.org.remove_participant(channel.band_room_id, band_id)
            except Exception:
                logger.exception("Couldn't remove %s from room %s", name, channel.band_room_id)
        await self.system_message(channel_id, f"**{name}** was removed from the channel.")
        await self.publish_channel(channel_id)

    # --- agents ---

    async def start_agent(self, profile: AgentProfile) -> None:
        assert profile.id is not None
        if not settings.anthropic_api_key:
            self.agents.status[profile.id] = {"state": "error", "error": "ANTHROPIC_API_KEY is not set"}
            await self.agent_status(profile.id, "error", "ANTHROPIC_API_KEY is not set")
            return
        owner = self.org.owner
        await self.agents.start(
            profile,
            owner_id=owner.id if owner else "",
        )

    async def _register_agent(
        self,
        *,
        name: str,
        role: str,
        instructions: str,
        model: str,
        created_by: int | None = None,
        created_by_agent: int | None = None,
        is_master: bool = False,
    ) -> AgentProfile:
        self._require_band()
        name = name.strip()
        if len(name) < 3:
            raise ServiceError("Agent name needs at least 3 characters", 422)
        with db() as session:
            if session.exec(select(AgentProfile).where(AgentProfile.name == name)).first():
                raise ServiceError(f"An agent named {name!r} already exists", 409)
        role_info = MASTER_ROLE if role == "master" else ROLES[role]
        try:
            band_id, api_key = await self.org.register_agent(
                name, f"{role_info['label']} agent in Band Slack. {role_info['description']}"
            )
            identity = await fetch_identity(api_key)
        except Exception as exc:
            raise ServiceError(f"Band couldn't register the agent: {describe_error(exc)}", 502) from exc
        profile = AgentProfile(
            name=identity.name or name,
            handle=identity.handle,
            role=role,
            instructions=instructions.strip(),
            model=model,
            band_agent_id=band_id,
            band_api_key_enc=encrypt_secret(api_key),
            is_master=is_master,
            created_by=created_by,
            created_by_agent=created_by_agent,
        )
        with db() as session:
            session.add(profile)
            session.commit()
            session.refresh(profile)
        await self.start_agent(profile)
        await bus.publish({"type": "agent", "agent": self.serialize_agent(profile)})
        return profile

    async def create_agent(
        self,
        *,
        name: str,
        role: str,
        instructions: str,
        model: str,
        channel_id: int | None = None,
        created_by: int | None = None,
        created_by_agent: int | None = None,
    ) -> AgentProfile:
        profile = await self._register_agent(
            name=name,
            role=role,
            instructions=instructions,
            model=model,
            created_by=created_by,
            created_by_agent=created_by_agent,
        )
        if channel_id is not None:
            await self.add_agent_to_channel(channel_id, profile)
        return profile

    async def delete_agent(self, profile: AgentProfile) -> None:
        if profile.is_master:
            raise ServiceError("The master agent can't be removed", 409)
        await self.agents.stop(profile.id)
        with db() as session:
            memberships = session.exec(
                select(ChannelMember).where(ChannelMember.member_type == "agent", ChannelMember.member_id == profile.id)
            ).all()
            channel_ids = [m.channel_id for m in memberships]
            for member in memberships:
                session.delete(member)
            # SQLite may reuse this row id for a future agent; nothing scoped to it may carry over.
            for memory in session.exec(select(AgentMemory).where(AgentMemory.agent_id == profile.id)).all():
                session.delete(memory)
            for schedule in session.exec(select(Schedule).where(Schedule.agent_id == profile.id)).all():
                schedule.enabled = False
                session.add(schedule)
            for record in session.exec(select(TaskSandbox).where(
                    TaskSandbox.task_id.startswith(f"agent-{profile.band_agent_id}-"))).all():
                session.delete(record)  # The cloud computer itself auto-stops and auto-deletes.
            for dm in session.exec(select(DirectMessage).where(DirectMessage.agent_id == profile.id)).all():
                dm.agent_id = None
                session.add(dm)
            session.delete(session.get(AgentProfile, profile.id))
            session.commit()
            rooms = [c.band_room_id for c in session.exec(select(Channel)).all() if c.band_room_id]
        for room_id in rooms:
            self.org.forget_participant(room_id, profile.band_agent_id)
        if self.org.online:
            try:
                await self.org.delete_agent(profile.band_agent_id)
            except Exception:
                logger.exception("Couldn't delete %s on Band", profile.name)
        for channel_id in channel_ids:
            await self.publish_channel(channel_id)
        await bus.publish({"type": "agent_deleted", "agent_id": profile.id})

    # --- messages ---

    async def _store(self, message: Message) -> dict[str, Any] | None:
        """Stores and broadcasts a message; Band messages already stored are skipped."""
        with db() as session:
            if message.band_message_id and session.exec(
                select(Message.id).where(Message.band_message_id == message.band_message_id)
            ).first():
                return None
            session.add(message)
            session.commit()
            session.refresh(message)
            payload = serialize_message(message)
        await bus.publish({"type": "message", "message": payload})
        return payload

    async def system_message(self, channel_id: int, content: str) -> None:
        await self._store(
            Message(channel_id=channel_id, author_type="system", author_name="System", content=content)
        )

    async def post_human_message(self, user: User, channel_id: int, content: str) -> dict[str, Any]:
        with db() as session:
            agents = session.exec(select(AgentProfile)).all()
            users = session.exec(select(User)).all()
            dm = session.get(DirectMessage, channel_id)
        if dm:
            # A direct message stays one-on-one: everything goes to its agent, mentioned or not.
            mentioned_agents = [a for a in agents if a.id == dm.agent_id]
            users = []
        else:
            mentioned_agents = [a for a in agents if _mentioned(content, a.name, a.handle)]
        mentions: list[dict[str, Any]] = [
            {"type": "agent", "id": a.id, "name": a.name} for a in mentioned_agents
        ] + [
            {"type": "human", "id": u.id, "name": u.display_name}
            for u in users
            if u.id != user.id and _mentioned(content, u.display_name, u.username)
        ]
        message = Message(
            channel_id=channel_id,
            author_type="human",
            author_id=user.id,
            author_name=user.display_name,
            content=content,
            mentions=mentions,
        )
        # Accept the message and its work atomically; Band relay cannot lose an accepted task.
        with db() as session:
            channel = session.get(Channel, channel_id)
            context = channel_context(session, channel_id) if mentioned_agents else ""
            session.add(message)
            session.flush()
            for agent in mentioned_agents:
                room_id = channel.band_room_id or ""
                session.add(AgentTask(id=uuid4().hex,
                    source_key=f"{agent.id}:{room_id}:app-message-{message.id}",
                    channel_id=channel_id, agent_id=agent.id, room_id=room_id,
                    prompt=f"**{user.display_name}:** {content}", context=context,
                    reply_to=self.org.owner.id if self.org.owner else ""))
                if not session.get(ChannelMember, (channel_id, "agent", agent.id)):
                    session.add(ChannelMember(channel_id=channel_id, member_type="agent", member_id=agent.id))
            session.commit()
            session.refresh(message)
            payload = serialize_message(message)
        await bus.publish({"type": "message", "message": payload})
        if mentioned_agents:
            await self.publish_channel(channel_id)
        asyncio.create_task(self._relay(channel_id, message.id, user.display_name, content, mentioned_agents))
        return payload

    async def _relay(
        self, channel_id: int, message_id: int, author: str, content: str, mentioned: list[AgentProfile]
    ) -> None:
        """Posts a human's message into the channel's Band room as the org owner."""
        if not self.org.online:
            if mentioned:
                await self.system_message(channel_id, "Band is offline; your agent task is saved locally.")
            return
        with db() as session:
            channel = session.get(Channel, channel_id)
            room_id = channel.band_room_id if channel else None
        if not room_id:
            return
        for profile in mentioned:
            await self.add_agent_to_channel(channel_id, profile)
        try:
            band_id = await self.org.send(
                room_id, f"**{author}:** {content}", [(a.band_agent_id, a.handle) for a in mentioned]
            )
        except Exception as exc:
            if mentioned:
                logger.exception("Relay to Band failed")
                await self.system_message(channel_id, f"Couldn't sync the message to Band: {describe_error(exc)}. Agent tasks remain saved locally.")
            else:
                logger.info("Band didn't accept an unmentioned message: %s", describe_error(exc))
            return
        with db() as session:
            stored = session.get(Message, message_id)
            if stored:
                stored.band_message_id = band_id
                session.add(stored)
                session.commit()

    def _strip_owner_mentions(self, content: str) -> str:
        owner = self.org.owner
        if owner:
            for ref in {owner.handle, owner.name}:
                if ref:
                    content = re.sub(rf"@{re.escape(ref)}(?![\w-])[,:]?\s*", "", content, flags=re.IGNORECASE)
        return content.strip()

    def _resolve_agent_mentions(self, refs: list[str]) -> tuple[list[dict[str, Any]], bool]:
        with db() as session:
            agents = session.exec(select(AgentProfile)).all()
        resolved: list[dict[str, Any]] = []
        to_humans = False
        for ref in refs:
            if self.org.is_owner_reference(ref):
                to_humans = True
                continue
            key = ref.strip().lstrip("@").lower()
            for agent in agents:
                if key in {agent.band_agent_id.lower(), agent.handle.lower(), agent.name.lower()}:
                    resolved.append({"type": "agent", "id": agent.id, "name": agent.name})
                    break
        return resolved, to_humans

    async def _store_agent_message(
        self, channel_id: int, profile: AgentProfile, content: str, refs: list[str], band_message_id: str | None
    ) -> None:
        with db() as session:
            is_member = session.get(ChannelMember, (channel_id, "agent", profile.id)) is not None
            if not is_member:
                session.add(ChannelMember(channel_id=channel_id, member_type="agent", member_id=profile.id))
                session.commit()
        if not is_member:
            await self.publish_channel(channel_id)
        resolved, to_humans = self._resolve_agent_mentions(refs)
        await self._store(
            Message(
                channel_id=channel_id,
                author_type="agent",
                author_id=profile.id,
                author_name=profile.name,
                content=self._strip_owner_mentions(content) if to_humans else content,
                mentions=resolved,
                to_humans=to_humans,
                band_message_id=band_message_id,
            )
        )

    # --- AgentSink ---

    async def agent_message(
        self,
        profile_id: int,
        room_id: str,
        content: str,
        mentions: list[str],
        band_message_id: str | None,
    ) -> None:
        with db() as session:
            channel = self._channel_for_room(session, room_id)
            profile = session.get(AgentProfile, profile_id)
        if channel is None or profile is None:
            logger.info("Ignoring agent message for unmapped room %s", room_id)
            return
        await self._store_agent_message(channel.id, profile, content, mentions, band_message_id)

    async def agent_typing(self, profile_id: int, room_id: str, active: bool) -> None:
        with db() as session:
            channel = self._channel_for_room(session, room_id)
        if channel is not None:
            await bus.publish(
                {"type": "typing", "channel_id": channel.id, "agent_id": profile_id, "active": active}
            )

    async def agent_status(self, profile_id: int, state: str, error: str | None) -> None:
        await bus.publish(
            {"type": "agent_status", "agent_id": profile_id, "status": {"state": state, "error": error}}
        )

    # --- Band -> app sync ---

    async def _poll_forever(self) -> None:
        started = datetime.now(timezone.utc)
        while True:
            await asyncio.sleep(settings.poll_seconds)
            with db() as session:
                rooms = [(c.id, c.band_room_id) for c in session.exec(select(Channel)).all() if c.band_room_id]
            for channel_id, room_id in rooms:
                try:
                    await self.sync_room(channel_id, room_id, self._poll_since.get(room_id, started))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Polling room %s failed: %s", room_id, describe_error(exc))

    async def sync_room(self, channel_id: int, room_id: str, since: datetime) -> None:
        """Brings in room messages posted outside this app (Band UI, external agents, SDK fallbacks)."""
        owner_id = self.org.owner.id if self.org.owner else None
        with db() as session:
            agents_by_band_id = {a.band_agent_id: a for a in session.exec(select(AgentProfile)).all()}
        for item in await self.org.messages_since(room_id, since):
            if item.inserted_at and item.inserted_at > self._poll_since.get(room_id, since):
                self._poll_since[room_id] = item.inserted_at
            if item.sender_id == owner_id:
                continue
            profile = agents_by_band_id.get(item.sender_id)
            if profile is not None:
                # Managed agents publish their answer locally before the worker sends
                # the Band copy. Mirroring that copy would race its send receipt.
                if profile.band_agent_id in self.agents.local_band_ids:
                    continue
                refs = re.findall(r"@([\w./-]+)", item.content)
                await self._store_agent_message(channel_id, profile, item.content, refs, item.id)
                continue
            with db() as session:
                content = display_names(session, item.content)
            await self._store(
                Message(
                    channel_id=channel_id,
                    author_type="external",
                    author_name=item.sender_name or "Band user",
                    content=self._strip_owner_mentions(content),
                    band_message_id=item.id,
                )
            )


service = AppService()

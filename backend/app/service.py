from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal

from band.runtime.custom_tools import CustomToolDef
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.agent_runner import AgentManager, current_room, describe_error, fetch_identity
from app.auth import encrypt_secret
from app.band_org import BandOrg
from app.config import settings
from app.events import bus
from app.models import AgentProfile, Channel, ChannelMember, Message, User, db
from app.roles import MASTER_ROLE, ROLES, role_label

logger = logging.getLogger(__name__)


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


def _mentioned(content: str, *names: str) -> bool:
    return any(
        name and re.search(rf"@{re.escape(name)}(?![\w-])", content, re.IGNORECASE)
        for name in names
    )


# The SDK names a custom tool after its input class: "Create_AgentInput" -> "create_agent".
class Create_AgentInput(BaseModel):
    """Create a new specialist AI agent on Band and add it to this room. Returns its handle so you can @mention it."""

    name: str = Field(min_length=3, max_length=40, description="Short display name, e.g. 'Market Researcher'")
    role: Literal["researcher", "writer", "coder", "critic", "coordinator", "custom"]
    instructions: str = Field(default="", max_length=4000, description="Focused instructions for this agent")


class List_AgentsInput(BaseModel):
    """List every agent in this workspace with its role, handle and whether it is already in this room."""


class AppService:
    """Keeps the app's channels, agents and messages in step with the org's Band account."""

    def __init__(self) -> None:
        self.org = BandOrg()
        self.agents = AgentManager(self)
        self._room_to_channel: dict[str, int] = {}
        self._poll_since: dict[str, datetime] = {}
        self._poller: asyncio.Task | None = None

    # --- lifecycle ---

    async def startup(self) -> None:
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
        if self._poller is not None:
            self._poller.cancel()
            await asyncio.gather(self._poller, return_exceptions=True)
        await self.agents.stop_all()
        await self.org.close()

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
            channel_ids = [c.id for c in session.exec(select(Channel)).all()]
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
        return {
            "id": channel.id,
            "name": channel.name,
            "topic": channel.topic,
            "band_room_id": channel.band_room_id,
            "human_ids": [m.member_id for m in members if m.member_type == "human"],
            "agent_ids": [m.member_id for m in members if m.member_type == "agent"],
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
            owner_name=owner.name if owner else "the humans",
            owner_handle=owner.handle if owner else "",
            tools=self._master_tools(profile) if profile.is_master else None,
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

    def _master_tools(self, master: AgentProfile) -> list[CustomToolDef]:
        async def create_agent(args: Create_AgentInput) -> dict[str, Any]:
            room_id = current_room.get()
            with db() as session:
                channel = self._channel_for_room(session, room_id) if room_id else None
            try:
                profile = await self.create_agent(
                    name=args.name,
                    role=args.role,
                    instructions=args.instructions,
                    model=master.model,
                    channel_id=channel.id if channel else None,
                    created_by_agent=master.id,
                )
            except ServiceError as exc:
                return {"error": exc.message}
            if channel and room_id:
                await self.system_message(
                    channel.id, f"{master.name} created **{profile.name}** ({role_label(profile.role)})."
                )
            return {
                "name": profile.name,
                "handle": profile.handle,
                "id": profile.band_agent_id,
                "role": profile.role,
                "in_this_room": channel is not None,
            }

        async def list_agents(_args: List_AgentsInput) -> list[dict[str, Any]]:
            room_id = current_room.get()
            with db() as session:
                channel = self._channel_for_room(session, room_id) if room_id else None
                in_room = (
                    {
                        m.member_id
                        for m in session.exec(
                            select(ChannelMember).where(
                                ChannelMember.channel_id == channel.id, ChannelMember.member_type == "agent"
                            )
                        ).all()
                    }
                    if channel
                    else set()
                )
                profiles = session.exec(select(AgentProfile).where(AgentProfile.id != master.id)).all()
            return [
                {
                    "name": p.name,
                    "handle": p.handle,
                    "id": p.band_agent_id,
                    "role": role_label(p.role),
                    "instructions": p.instructions[:200],
                    "in_this_room": p.id in in_room,
                }
                for p in profiles
            ]

        return [(Create_AgentInput, create_agent), (List_AgentsInput, list_agents)]

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
        payload = await self._store(message)
        assert payload is not None
        asyncio.create_task(self._relay(channel_id, message.id, user.display_name, content, mentioned_agents))
        return payload

    async def _relay(
        self, channel_id: int, message_id: int, author: str, content: str, mentioned: list[AgentProfile]
    ) -> None:
        """Posts a human's message into the channel's Band room as the org owner."""
        if not self.org.online:
            if mentioned:
                await self.system_message(channel_id, f"Agents can't be reached: {self.org.error or 'not connected to Band'}.")
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
                await self.system_message(channel_id, f"Couldn't deliver the message to Band: {describe_error(exc)}")
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
                refs = re.findall(r"@([\w./-]+)", item.content)
                await self._store_agent_message(channel_id, profile, item.content, refs, item.id)
                continue
            await self._store(
                Message(
                    channel_id=channel_id,
                    author_type="external",
                    author_name=item.sender_name or "Band user",
                    content=self._strip_owner_mentions(item.content),
                    band_message_id=item.id,
                )
            )


service = AppService()

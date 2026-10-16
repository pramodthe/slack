"""Agents grow their team: new visible teammates (registered by the API process) and outside Band agents."""
from __future__ import annotations

import asyncio
import logging

from band.client.rest import AsyncRestClient, ChatMessageRequest, ChatMessageRequestMentionsItem, aclose_rest_client
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.auth import decrypt_secret
from app.config import settings
from app.persistence.models import (AgentProfile, AgentTask, DirectMessage, Message, OutsideAgent, OutsideHandoff,
                                    TeammateRequest, db)
from app.tasks.queue import MAX_HANDOFFS
from app.workspace.roles import ROLES, role_label

logger = logging.getLogger(__name__)

CREATE_WAIT = 30.0
REGISTER_WAIT = 60.0
POLL = 0.5


def _role_key(role: str) -> str | None:
    wanted = role.strip().lower()
    return next((key for key, info in ROLES.items() if wanted in (key, info["label"].lower())), None)


def _created(name: str, role: str) -> dict:
    return {"status": "created", "teammate": name, "role": role_label(role),
            "note": f"{name} is now a visible teammate in this channel. Hand off with ask_teammate."}


def _request(task: AgentTask, *, name: str, role: str, instructions: str, call_id: str) -> dict | int:
    """A finished answer, or the id of the request to wait for."""
    name = name.strip().lstrip("@")
    key = _role_key(role)
    if key is None:
        return {"error": f"Unknown role {role!r}. Choose one of: {', '.join(ROLES)}."}
    if not 3 <= len(name) <= 40:
        return {"error": "Give the teammate a name of 3-40 characters."}
    source_key = f"{task.id}:{call_id}"
    with db() as session:
        if session.get(DirectMessage, task.channel_id):
            return {"error": "This is a private one-on-one conversation, so teammates can't join it."}
        if existing := session.exec(select(TeammateRequest).where(TeammateRequest.source_key == source_key)).first():
            return existing.id
        same = session.exec(select(AgentProfile).where(func.lower(AgentProfile.name) == name.lower())).first()
        if same:
            return {"status": "exists", "teammate": same.name, "role": role_label(same.role),
                    "note": f"{same.name} already exists; reuse it with ask_teammate instead of creating another."}
        in_progress = session.exec(select(TeammateRequest).where(
            func.lower(TeammateRequest.name) == name.lower(),
            TeammateRequest.status.in_(["pending", "creating"]))).first()
        if in_progress:
            return in_progress.id
        made = session.exec(select(func.count()).select_from(AgentProfile).where(
            AgentProfile.created_by_agent != None)).one()  # noqa: E711
        waiting = session.exec(select(func.count()).select_from(TeammateRequest).where(
            TeammateRequest.status.in_(["pending", "creating"]))).one()
        if made + waiting >= settings.max_agent_teammates:
            return {"error": f"Agents have already created {settings.max_agent_teammates} teammates, the workspace "
                             "limit. Reuse an existing teammate or ask a person to create one."}
        requester = session.get(AgentProfile, task.agent_id)
        request = TeammateRequest(source_key=source_key, task_id=task.id, channel_id=task.channel_id,
                                  requested_by_agent_id=task.agent_id, name=name, role=key,
                                  instructions=instructions.strip()[:4000],
                                  model=requester.model if requester else settings.default_model)
        session.add(request)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return session.exec(select(TeammateRequest).where(TeammateRequest.source_key == source_key)).one().id
        return request.id


async def create_teammate(task: AgentTask, *, name: str, role: str, instructions: str, call_id: str,
                          wait: float = CREATE_WAIT, register_wait: float = REGISTER_WAIT) -> dict:
    """Ask the API process to register a teammate, then wait for the outcome."""
    outcome = _request(task, name=name, role=role, instructions=instructions, call_id=call_id)
    if isinstance(outcome, dict):
        return outcome
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        with db() as session:
            request = session.get(TeammateRequest, outcome)
            agent = session.get(AgentProfile, request.agent_id) if request.agent_id else None
        if request.status == "created" and agent:
            return _created(agent.name, agent.role)
        if request.status == "failed":
            return {"error": f"Couldn't create {request.name}: {request.error or 'unknown error'}"}
        if asyncio.get_running_loop().time() >= deadline:
            with db() as session:
                # Unclaimed requests expire so the app server can't create the teammate after the agent moved on.
                expired = session.exec(update(TeammateRequest).where(
                    TeammateRequest.id == outcome, TeammateRequest.status == "pending").values(
                    status="failed", error="The app server didn't pick up the request; try again later"))
                session.commit()
            if expired.rowcount:
                return {"error": f"Couldn't create {request.name}: the app server didn't respond. Try again later."}
            if register_wait <= 0:
                return {"status": "creating", "teammate": request.name,
                        "note": "Band is still registering it. Try ask_teammate in a minute."}
            deadline, register_wait = deadline + register_wait, 0
        await asyncio.sleep(POLL)


def _matches(value: str, *candidates: str) -> bool:
    wanted = value.strip().lstrip("@").lower()
    return any(wanted == c.lower() for c in candidates if c)


def outside_agent(session, task: AgentTask, teammate: str) -> OutsideAgent | None:
    """An outside agent in this channel with that name or handle, unless a local teammate has the name."""
    if any(_matches(teammate, a.name, a.handle) for a in session.exec(select(AgentProfile)).all()):
        return None
    return next((o for o in session.exec(select(OutsideAgent).where(OutsideAgent.channel_id == task.channel_id)).all()
                 if _matches(teammate, o.name, o.handle, o.handle.rsplit("/", 1)[-1])), None)


async def ask_outside(task: AgentTask, outside: OutsideAgent, request: str, call_id: str) -> dict:
    """Post a request to an outside agent in the Band room, at most once even if the tool call replays."""
    request = request.strip()
    if not request or len(request) > 8000:
        return {"error": "Provide a request of 1-8000 characters."}
    if not task.room_id:
        return {"error": "This channel has no Band room, so outside agents can't be reached."}
    source_key = f"{task.id}:{call_id}"
    with db() as session:
        if existing := session.get(OutsideHandoff, source_key):
            return _sent(outside, existing.status)
        if len(session.exec(select(OutsideHandoff).where(OutsideHandoff.task_id == task.id)).all()) >= MAX_HANDOFFS:
            return {"error": f"This task already sent {MAX_HANDOFFS} requests to outside agents."}
        sender = session.get(AgentProfile, task.agent_id)
        session.add_all([
            OutsideHandoff(source_key=source_key, task_id=task.id, channel_id=task.channel_id,
                           band_id=outside.band_id, request=request),
            Message(channel_id=task.channel_id, author_type="agent", author_id=sender.id, author_name=sender.name,
                    content=f"@{outside.name} {request}", to_humans=False, band_message_id=f"handoff:{call_id}",
                    mentions=[{"type": "external", "id": None, "name": outside.name}]),
        ])
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return _sent(outside, session.get(OutsideHandoff, source_key).status)
    content = (f"@{outside.name} {request}\n\n"
               f"(From {sender.name} in Band Slack. Reply in this room and mention @{sender.name} so it gets your answer.)")
    client = AsyncRestClient(api_key=decrypt_secret(sender.band_api_key_enc), base_url=settings.band_rest_url)
    try:
        response = await client.agent_api_messages.create_agent_chat_message(
            task.room_id, message=ChatMessageRequest(content=content, mentions=[
                ChatMessageRequestMentionsItem(id=outside.band_id)]),
            request_options={"max_retries": 0, "timeout_in_seconds": 30})
        status, band_message_id = "sent", response.data.id
    except Exception:
        logger.exception("Outside hand-off from task %s may not have reached Band", task.id)
        status, band_message_id = "unknown", ""
    finally:
        await aclose_rest_client(client)
    with db() as session:
        session.exec(update(OutsideHandoff).where(OutsideHandoff.source_key == source_key).values(
            status=status, band_message_id=band_message_id))
        session.commit()
    return _sent(outside, status)


def outside_reply(session, agent_id: int, channel_id: int, sender_id: str, prompt: str) -> str:
    """Frame an outside agent's mention as the answer to this agent's latest request to it, if there was one."""
    handoff = session.exec(select(OutsideHandoff).where(
        OutsideHandoff.channel_id == channel_id, OutsideHandoff.band_id == sender_id,
        OutsideHandoff.status != "unknown").order_by(OutsideHandoff.created_at.desc())).first()
    origin = session.get(AgentTask, handoff.task_id) if handoff else None
    if origin is None or origin.agent_id != agent_id:
        return prompt
    outsider = session.get(OutsideAgent, (channel_id, sender_id))
    name = outsider.name if outsider else "The outside agent"
    return (f"{name} (an outside Band agent) answered the request you sent it: \"{handoff.request}\"\n"
            f"You sent that request while working on: \"{origin.prompt[:2000]}\"\n\n"
            f"{name}'s reply:\n{prompt}\n\n"
            "Use the reply to finish that work and answer the humans.")


def _sent(outside: OutsideAgent, status: str) -> dict:
    if status == "unknown":
        return {"error": f"Couldn't confirm the request reached {outside.name} through Band. Don't resend it; "
                         "tell the humans it may not have arrived."}
    return {"status": "handed_off", "teammate": outside.name, "outside": True,
            "note": (f"{outside.name} runs outside this app. Its reply appears in the channel, and it comes back "
                     "to you as a new task only if it mentions you. End your turn with a one-line status.")}

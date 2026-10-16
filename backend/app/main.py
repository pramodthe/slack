from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from sqlalchemy import case, update

from app.auth import current_user, hash_password, issue_token, user_id_from_token, verify_password
from app.config import FRONTEND_DIST, MODELS, settings
from app.events import bus
from app.models import AgentMemory, AgentTask, AgentProfile, Channel, ChannelMember, Message, User, db, get_session, init_db, utcnow
from app.roles import ROLES
from app.tasks import serialize_task, update_task
from app.computer import sandbox_record
from app.service import ServiceError, serialize_message, serialize_user, service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "httpx2"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    with db() as session:
        if not session.exec(select(Channel)).first():
            session.add(Channel(name="general", topic="Humans and agents, all together"))
            session.commit()
    startup = asyncio.create_task(service.startup())
    yield
    startup.cancel()
    await asyncio.gather(startup, return_exceptions=True)
    await service.shutdown()


app = FastAPI(title="Band Slack", lifespan=lifespan)


@app.exception_handler(ServiceError)
async def service_error(_request: Request, exc: ServiceError):
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


# --- auth ---


class SignupBody(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=48)
    password: str = Field(min_length=6, max_length=128)


class LoginBody(BaseModel):
    username: str
    password: str


def _join_general(session: Session, user: User) -> None:
    general = session.exec(select(Channel).where(Channel.name == "general")).first()
    if general:
        session.add(ChannelMember(channel_id=general.id, member_type="human", member_id=user.id))


@app.post("/api/auth/signup")
async def signup(body: SignupBody, session: Session = Depends(get_session)):
    username = body.username.lower()
    if session.exec(select(User).where(User.username == username)).first():
        raise HTTPException(409, "That username is taken")
    user = User(username=username, display_name=body.display_name.strip(), password_hash=hash_password(body.password))
    session.add(user)
    session.commit()
    session.refresh(user)
    _join_general(session, user)
    session.commit()
    await bus.publish({"type": "user", "user": serialize_user(user)})
    general = session.exec(select(Channel).where(Channel.name == "general")).first()
    if general:
        await service.publish_channel(general.id)
    return {"token": issue_token(user.id), "user": serialize_user(user)}


@app.post("/api/auth/login")
async def login(body: LoginBody, session: Session = Depends(get_session)):
    user = session.exec(select(User).where(User.username == body.username.lower())).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Wrong username or password")
    return {"token": issue_token(user.id), "user": serialize_user(user)}


@app.get("/api/bootstrap")
async def bootstrap(user: User = Depends(current_user), session: Session = Depends(get_session)):
    return {
        "me": serialize_user(user),
        "users": [serialize_user(u) for u in session.exec(select(User)).all()],
        "channels": [service.serialize_channel(session, c) for c in session.exec(select(Channel).order_by(Channel.name)).all()],
        "agents": [service.serialize_agent(a) for a in session.exec(select(AgentProfile)).all()],
        "roles": [{"id": k, **v} for k, v in ROLES.items()],
        "models": MODELS,
        "band": service.org.status(),
        "anthropic_configured": bool(settings.anthropic_api_key),
    }


# --- channels ---


class ChannelBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    topic: str = Field(default="", max_length=200)


def _channel_or_404(session: Session, channel_id: int) -> Channel:
    channel = session.get(Channel, channel_id)
    if not channel:
        raise HTTPException(404, "Channel not found")
    return channel


@app.post("/api/channels")
async def create_channel(body: ChannelBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    name = re.sub(r"[^a-z0-9_-]+", "-", body.name.strip().lower()).strip("-")
    if not name:
        raise HTTPException(422, "Channel name needs letters or numbers")
    return await service.create_channel(name, body.topic.strip(), user)


@app.post("/api/channels/{channel_id}/join")
async def join_channel(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id)
    if not session.get(ChannelMember, (channel_id, "human", user.id)):
        session.add(ChannelMember(channel_id=channel_id, member_type="human", member_id=user.id))
        session.commit()
        await service.publish_channel(channel_id)
    return {"ok": True}


@app.post("/api/channels/{channel_id}/leave")
async def leave_channel(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    member = session.get(ChannelMember, (channel_id, "human", user.id))
    if member:
        session.delete(member)
        session.commit()
        await service.publish_channel(channel_id)
    return {"ok": True}


class ChannelAgentBody(BaseModel):
    agent_id: int


@app.post("/api/channels/{channel_id}/agents")
async def add_channel_agent(
    channel_id: int, body: ChannelAgentBody, _user: User = Depends(current_user), session: Session = Depends(get_session)
):
    _channel_or_404(session, channel_id)
    profile = _agent_or_404(session, body.agent_id)
    await service.add_agent_to_channel(channel_id, profile)
    return {"ok": True}


@app.delete("/api/channels/{channel_id}/agents/{agent_id}")
async def remove_channel_agent(
    channel_id: int, agent_id: int, _user: User = Depends(current_user), session: Session = Depends(get_session)
):
    _channel_or_404(session, channel_id)
    await service.remove_agent_from_channel(channel_id, _agent_or_404(session, agent_id))
    return {"ok": True}


@app.get("/api/channels/{channel_id}/messages")
async def list_messages(
    channel_id: int,
    before: int | None = None,
    limit: int = 100,
    _user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    _channel_or_404(session, channel_id)
    query = select(Message).where(Message.channel_id == channel_id)
    if before:
        query = query.where(Message.id < before)
    rows = session.exec(query.order_by(Message.id.desc()).limit(min(limit, 200))).all()
    return [serialize_message(m) for m in reversed(rows)]


class MessageBody(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


@app.post("/api/channels/{channel_id}/messages")
async def post_message(
    channel_id: int, body: MessageBody, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    _channel_or_404(session, channel_id)
    if not session.get(ChannelMember, (channel_id, "human", user.id)):
        session.add(ChannelMember(channel_id=channel_id, member_type="human", member_id=user.id))
        session.commit()
        await service.publish_channel(channel_id)
    return await service.post_human_message(user, channel_id, body.content.strip())


# --- agents ---


class AgentCreateBody(BaseModel):
    name: str = Field(min_length=3, max_length=40)
    role: str = "custom"
    instructions: str = Field(default="", max_length=6000)
    model: str = settings.default_model
    channel_id: int | None = None


class AgentUpdateBody(BaseModel):
    role: str | None = None
    instructions: str | None = Field(default=None, max_length=6000)
    model: str | None = None


def _validate_role_and_model(role: str | None, model: str | None) -> None:
    if role is not None and role not in ROLES:
        raise HTTPException(422, f"Unknown role {role!r}")
    if model is not None and model not in {m["id"] for m in MODELS}:
        raise HTTPException(422, f"Unknown model {model!r}")


@app.post("/api/agents")
async def create_agent(body: AgentCreateBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _validate_role_and_model(body.role, body.model)
    if body.channel_id is not None:
        _channel_or_404(session, body.channel_id)
    profile = await service.create_agent(
        name=body.name,
        role=body.role,
        instructions=body.instructions,
        model=body.model,
        channel_id=body.channel_id,
        created_by=user.id,
    )
    return service.serialize_agent(profile)


def _agent_or_404(session: Session, agent_id: int) -> AgentProfile:
    profile = session.get(AgentProfile, agent_id)
    if not profile:
        raise HTTPException(404, "Agent not found")
    return profile


@app.patch("/api/agents/{agent_id}")
async def update_agent(
    agent_id: int, body: AgentUpdateBody, _user: User = Depends(current_user), session: Session = Depends(get_session)
):
    _validate_role_and_model(body.role, body.model)
    profile = _agent_or_404(session, agent_id)
    if body.role is not None and not profile.is_master:
        profile.role = body.role
    if body.instructions is not None:
        profile.instructions = body.instructions.strip()
    if body.model is not None:
        profile.model = body.model
    session.add(profile)
    session.commit()
    await service.start_agent(profile)
    payload = service.serialize_agent(profile)
    await bus.publish({"type": "agent", "agent": payload})
    return payload


@app.post("/api/agents/{agent_id}/restart")
async def restart_agent(agent_id: int, _user: User = Depends(current_user), session: Session = Depends(get_session)):
    profile = _agent_or_404(session, agent_id)
    await service.start_agent(profile)
    return service.serialize_agent(profile)


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: int, _user: User = Depends(current_user), session: Session = Depends(get_session)):
    await service.delete_agent(_agent_or_404(session, agent_id))
    return {"ok": True}


# --- durable task controls ---


@app.get("/api/channels/{channel_id}/tasks")
async def list_tasks(channel_id: int, _user: User = Depends(current_user), session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id)
    rows = session.exec(select(AgentTask).where(AgentTask.channel_id == channel_id)
                        .order_by(AgentTask.created_at.desc()).limit(30)).all()
    return [serialize_task(task) for task in rows]


def _task_or_404(session: Session, task_id: str) -> AgentTask:
    task = session.get(AgentTask, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


def _require_task_member(session: Session, task: AgentTask, user: User) -> None:
    if not session.get(ChannelMember, (task.channel_id, "human", user.id)):
        raise HTTPException(403, "Join the channel to control its tasks")


@app.get("/api/tasks/{task_id}")
async def task_detail(task_id: str, _user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _task_or_404(session, task_id)
    return {**serialize_task(task), "result": task.result, "artifacts": task.artifacts}


@app.get("/api/tasks/{task_id}/computer")
async def task_computer(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _task_or_404(session, task_id)
    _require_task_member(session, task, user)
    row = sandbox_record(task_id)
    return JSONResponse({
        "configured": bool(settings.daytona_api_key),
        "status": row.status if row else "not_started",
        "sandbox_id": row.sandbox_id if row else None,
        "image": row.latest_image if row else "",
        "width": row.width if row else 0,
        "height": row.height if row else 0,
        "last_action": row.last_action if row else "",
        "updated_at": row.updated_at.isoformat() if row else None,
        "error": row.error if row else None,
        "dashboard_url": "https://app.daytona.io/dashboard/sandboxes",
    }, headers={"Cache-Control": "no-store"})


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _task_or_404(session, task_id)
    _require_task_member(session, task, user)
    if task.status not in {"queued", "running"}:
        raise HTTPException(409, "Only queued or running tasks can be cancelled")
    session.exec(update(AgentTask).where(AgentTask.id == task_id, AgentTask.status.in_(["queued", "running"]))
                 .values(cancel_requested=True,
                         status=case((AgentTask.status == "queued", "cancelled"), else_=AgentTask.status),
                         progress=case((AgentTask.status == "queued", "Cancelled"), else_="Cancellation requested"),
                         updated_at=utcnow()))
    session.commit()
    session.expire_all()
    return serialize_task(session.get(AgentTask, task_id))


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _task_or_404(session, task_id)
    _require_task_member(session, task, user)
    if task.status not in {"failed", "cancelled"}:
        raise HTTPException(409, "Only failed or cancelled tasks can be retried")
    session.exec(update(AgentTask).where(AgentTask.id == task_id, AgentTask.status.in_(["failed", "cancelled"]))
                 .values(status="queued", cancel_requested=False, error=None,
                         progress="Queued to resume saved progress", updated_at=utcnow()))
    session.commit()
    session.expire_all()
    return serialize_task(session.get(AgentTask, task_id))


@app.get("/api/channels/{channel_id}/agents/{agent_id}/memories")
async def list_memories(channel_id: int, agent_id: int, _user: User = Depends(current_user),
                        session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id)
    return session.exec(select(AgentMemory).where(AgentMemory.agent_id == agent_id,
                        AgentMemory.channel_id == channel_id)).all()


@app.delete("/api/channels/{channel_id}/agents/{agent_id}/memories/{key}")
async def forget_memory(channel_id: int, agent_id: int, key: str, user: User = Depends(current_user),
                         session: Session = Depends(get_session)):
    if not session.get(ChannelMember, (channel_id, "human", user.id)):
        raise HTTPException(403, "Join the channel to manage its memories")
    memory = session.get(AgentMemory, (agent_id, channel_id, key))
    if memory is None:
        raise HTTPException(404, "Memory not found")
    session.delete(memory)
    session.commit()
    return {"ok": True}


# --- realtime ---


@app.websocket("/ws")
async def websocket(ws: WebSocket, token: str = ""):
    user_id = user_id_from_token(token)
    if user_id is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    await bus.connect(user_id, ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await bus.disconnect(user_id, ws)


@app.get("/api/health")
async def health():
    return {"ok": True, "band": service.org.status()}


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str):
        candidate = FRONTEND_DIST / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")

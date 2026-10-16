from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from sqlalchemy import case, update

from app.auth import current_user, hash_password, issue_token, user_id_from_token, verify_password
from app.config import FRONTEND_DIST, MODELS, settings
from app.events import bus
from app.models import (AgentMemory, AgentTask, AgentProfile, Channel, ChannelMember, DirectMessage, Message,
                        TaskApproval, User, db, get_session, init_db, utcnow)
from app.roles import ROLES
from app.schedules import cancel_schedule, channel_schedules, describe, serialize_schedule
from app.tasks import decide_approval, serialize_task, update_task
from daytona import DaytonaError

from app.computer import (TRANSFER_LIMIT, ComputerUnavailable, TaskComputer, active_control, computer_key,
                          release_control, sandbox_record, take_control)
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
    dms = {dm.channel_id: dm for dm in session.exec(select(DirectMessage)).all()}
    visible = [c for c in session.exec(select(Channel).order_by(Channel.name)).all()
               if c.id not in dms or (dms[c.id].user_id == user.id and dms[c.id].agent_id is not None)]
    return {
        "me": serialize_user(user),
        "users": [serialize_user(u) for u in session.exec(select(User)).all()],
        "channels": [service.serialize_channel(session, c) for c in visible],
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


def _channel_or_404(session: Session, channel_id: int, user: User) -> Channel:
    channel = session.get(Channel, channel_id)
    dm = session.get(DirectMessage, channel_id)
    if not channel or (dm and dm.user_id != user.id):
        raise HTTPException(404, "Channel not found")
    return channel


def _reject_dm(session: Session, channel_id: int, detail: str) -> None:
    if session.get(DirectMessage, channel_id):
        raise HTTPException(409, detail)


@app.post("/api/channels")
async def create_channel(body: ChannelBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    name = re.sub(r"[^a-z0-9_-]+", "-", body.name.strip().lower()).strip("-")
    if not name:
        raise HTTPException(422, "Channel name needs letters or numbers")
    return await service.create_channel(name, body.topic.strip(), user)


@app.post("/api/channels/{channel_id}/join")
async def join_channel(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id, user)
    if not session.get(ChannelMember, (channel_id, "human", user.id)):
        session.add(ChannelMember(channel_id=channel_id, member_type="human", member_id=user.id))
        session.commit()
        await service.publish_channel(channel_id)
    return {"ok": True}


@app.post("/api/channels/{channel_id}/leave")
async def leave_channel(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id, user)
    _reject_dm(session, channel_id, "You can't leave a direct message")
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
    channel_id: int, body: ChannelAgentBody, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    _channel_or_404(session, channel_id, user)
    _reject_dm(session, channel_id, "Direct messages are one person and one agent")
    profile = _agent_or_404(session, body.agent_id)
    await service.add_agent_to_channel(channel_id, profile)
    return {"ok": True}


@app.delete("/api/channels/{channel_id}/agents/{agent_id}")
async def remove_channel_agent(
    channel_id: int, agent_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    _channel_or_404(session, channel_id, user)
    _reject_dm(session, channel_id, "Direct messages are one person and one agent")
    await service.remove_agent_from_channel(channel_id, _agent_or_404(session, agent_id))
    return {"ok": True}


@app.get("/api/channels/{channel_id}/messages")
async def list_messages(
    channel_id: int,
    before: int | None = None,
    limit: int = 100,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    _channel_or_404(session, channel_id, user)
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
    _channel_or_404(session, channel_id, user)
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
    channel_id = body.channel_id
    if channel_id is not None:
        _channel_or_404(session, channel_id, user)
        if session.get(DirectMessage, channel_id):
            channel_id = None
    profile = await service.create_agent(
        name=body.name,
        role=body.role,
        instructions=body.instructions,
        model=body.model,
        channel_id=channel_id,
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


@app.post("/api/agents/{agent_id}/dm")
async def open_dm(agent_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    return await service.open_dm(user, _agent_or_404(session, agent_id))


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: int, _user: User = Depends(current_user), session: Session = Depends(get_session)):
    await service.delete_agent(_agent_or_404(session, agent_id))
    return {"ok": True}


# --- durable task controls ---


@app.get("/api/channels/{channel_id}/tasks")
async def list_tasks(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id, user)
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
async def task_detail(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _task_or_404(session, task_id)
    dm = session.get(DirectMessage, task.channel_id)
    if dm and dm.user_id != user.id:
        raise HTTPException(404, "Task not found")
    return {**serialize_task(task), "result": task.result, "artifacts": task.artifacts}


@app.get("/api/tasks/{task_id}/computer")
async def task_computer(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _task_or_404(session, task_id)
    _require_task_member(session, task, user)
    # Computers persist per agent and channel; tasks from before that change kept their own.
    row = sandbox_record(computer_key(task)) or sandbox_record(task_id)
    return JSONResponse({
        "control": _control_json(active_control(computer_key(task)), user),
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


NO_STORE = {"Cache-Control": "no-store"}


@app.get("/api/channels/{channel_id}/computers")
async def channel_computers(channel_id: int, user: User = Depends(current_user),
                            session: Session = Depends(get_session)):
    """Each agent's computer in this channel, without screenshots, so clients can poll it cheaply."""
    _channel_or_404(session, channel_id, user)
    computers = []
    for agent_id in session.exec(select(AgentTask.agent_id).where(AgentTask.channel_id == channel_id).distinct()).all():
        latest = session.exec(select(AgentTask).where(AgentTask.channel_id == channel_id, AgentTask.agent_id == agent_id)
                              .order_by(AgentTask.created_at.desc())).first()
        row = sandbox_record(computer_key(latest))
        if row is None:
            continue
        working = session.exec(select(AgentTask.id).where(AgentTask.channel_id == channel_id,
                               AgentTask.agent_id == agent_id, AgentTask.status == "running")).first()
        computers.append({"agent_id": agent_id, "task_id": latest.id, "status": row.status,
                          "last_action": row.last_action, "updated_at": row.updated_at.isoformat(),
                          "agent_working": working is not None})
    computers.sort(key=lambda c: c["updated_at"], reverse=True)
    return JSONResponse(computers, headers=NO_STORE)


def computer_factory(task: AgentTask) -> TaskComputer:
    """Tests replace this to use a fake Daytona client."""
    return TaskComputer(task)


def _control_json(control, user: User) -> dict | None:
    if control is None:
        return None
    return {"user_name": control.user_name, "until": control.until.isoformat(), "mine": control.user_id == user.id}


def _member_task(session: Session, task_id: str, user: User) -> AgentTask:
    task = _task_or_404(session, task_id)
    _require_task_member(session, task, user)
    return task


def _agent_name(session: Session, task: AgentTask) -> str:
    agent = session.get(AgentProfile, task.agent_id)
    return agent.name if agent else "the agent"


async def _on_computer(task: AgentTask, operation):
    """Run a person's request on the agent's computer without stopping it afterwards."""
    computer = computer_factory(task)
    try:
        return await operation(computer)
    except ComputerUnavailable as exc:
        raise HTTPException(409, str(exc))
    except DaytonaError as exc:
        # Daytona errors can include request details; show only the type.
        logger.warning("Daytona request for task %s failed: %s", task.id, type(exc).__name__)
        raise HTTPException(502, f"Daytona request failed ({type(exc).__name__})")
    finally:
        await computer.detach()


class LiveBody(BaseModel):
    control: bool = False


@app.post("/api/tasks/{task_id}/computer/live")
async def computer_live(task_id: str, body: LiveBody, user: User = Depends(current_user),
                        session: Session = Depends(get_session)):
    """A short-lived noVNC link. View-only is a client setting, so the link is limited to channel members."""
    task = _member_task(session, task_id, user)
    view = await _on_computer(task, lambda computer: computer.live_view())
    key = computer_key(task)
    if body.control:
        control = take_control(key, user_id=user.id, user_name=user.display_name)
        await service.system_message(task.channel_id, f"{user.display_name} took control of {_agent_name(session, task)}'s "
                                                      "computer. The agent waits until it is handed back.")
    else:
        control = active_control(key)
    return JSONResponse({**view, "control": _control_json(control, user)}, headers=NO_STORE)


@app.delete("/api/tasks/{task_id}/computer/control")
async def computer_release(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _member_task(session, task_id, user)
    if active_control(computer_key(task)):
        release_control(computer_key(task), user_name=user.display_name)
        await service.system_message(task.channel_id, f"{user.display_name} handed {_agent_name(session, task)}'s "
                                                      "computer back.")
    return {"ok": True}


@app.get("/api/tasks/{task_id}/computer/files")
async def computer_files(task_id: str, path: str = "", user: User = Depends(current_user),
                         session: Session = Depends(get_session)):
    task = _member_task(session, task_id, user)
    entries = await _on_computer(task, lambda computer: computer.list_files(path))
    return JSONResponse(entries, headers=NO_STORE)


@app.get("/api/tasks/{task_id}/computer/files/download")
async def computer_download(task_id: str, path: str, user: User = Depends(current_user),
                            session: Session = Depends(get_session)):
    task = _member_task(session, task_id, user)
    content = await _on_computer(task, lambda computer: computer.download(path))
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", path.rstrip("/").rsplit("/", 1)[-1]) or "download"
    return Response(content, media_type="application/octet-stream",
                    headers={**NO_STORE, "Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/api/tasks/{task_id}/computer/files")
async def computer_upload(task_id: str, name: str, request: Request, user: User = Depends(current_user),
                          session: Session = Depends(get_session)):
    task = _member_task(session, task_id, user)
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > TRANSFER_LIMIT:
            raise HTTPException(413, "Uploads support files up to 25 MB")
    path = await _on_computer(task, lambda computer: computer.upload(name, bytes(content)))
    # Agents read the channel, so this tells them where the file is.
    await service.system_message(task.channel_id, f"{user.display_name} uploaded {name} to "
                                                  f"{_agent_name(session, task)}'s computer at {path}.")
    return {"path": path}


@app.post("/api/tasks/{task_id}/computer/stop")
async def computer_stop(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _member_task(session, task_id, user)
    if session.exec(select(AgentTask.id).where(AgentTask.agent_id == task.agent_id,
                    AgentTask.channel_id == task.channel_id, AgentTask.status == "running")).first():
        raise HTTPException(409, "The agent is using this computer. Cancel its task first.")
    release_control(computer_key(task), user_name=user.display_name)
    computer = computer_factory(task)
    await computer.close()
    return {"ok": True}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    task = _task_or_404(session, task_id)
    _require_task_member(session, task, user)
    if task.status not in {"queued", "running", "waiting"}:
        raise HTTPException(409, "Only queued, running, or waiting tasks can be cancelled")
    idle = AgentTask.status.in_(["queued", "waiting"])
    session.exec(update(AgentTask).where(AgentTask.id == task_id, AgentTask.status.in_(["queued", "running", "waiting"]))
                 .values(cancel_requested=True,
                         status=case((idle, "cancelled"), else_=AgentTask.status),
                         progress=case((idle, "Cancelled"), else_="Cancellation requested"),
                         updated_at=utcnow()))
    # Close the task's open approval requests so their cards disappear and cannot be approved later.
    session.exec(update(TaskApproval).where(TaskApproval.task_id == task_id, TaskApproval.status == "pending")
                 .values(status="denied", note="Task cancelled", decided_by=user.display_name, decided_at=utcnow()))
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


@app.get("/api/channels/{channel_id}/approvals")
async def list_approvals(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id, user)
    return session.exec(select(TaskApproval).join(AgentTask, AgentTask.id == TaskApproval.task_id).where(
        TaskApproval.channel_id == channel_id, TaskApproval.status == "pending", AgentTask.status == "waiting")
        .order_by(TaskApproval.created_at)).all()


class DecisionBody(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=2000)


@app.post("/api/approvals/{approval_id}")
async def decide(approval_id: str, body: DecisionBody, user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    approval = session.get(TaskApproval, approval_id)
    if approval is None:
        raise HTTPException(404, "Approval request not found")
    if not session.get(ChannelMember, (approval.channel_id, "human", user.id)):
        raise HTTPException(403, "Join the channel to decide its approvals")
    decided = decide_approval(approval_id, approve=body.approve, decided_by=user.display_name, note=body.note)
    if decided is None:
        raise HTTPException(409, "This request was already decided or its task is no longer waiting")
    verdict = "approved" if body.approve else "denied"
    await service.system_message(decided.channel_id, f"{user.display_name} {verdict}: {decided.action}"
                                 + (f" ({body.note})" if body.note else ""))
    return decided


@app.get("/api/channels/{channel_id}/schedules")
async def list_schedules(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id, user)
    return [serialize_schedule(s) for s in channel_schedules(channel_id)]


@app.delete("/api/channels/{channel_id}/schedules/{schedule_id}")
async def delete_schedule(channel_id: int, schedule_id: int, user: User = Depends(current_user),
                          session: Session = Depends(get_session)):
    if not session.get(ChannelMember, (channel_id, "human", user.id)):
        raise HTTPException(403, "Join the channel to manage its schedules")
    cancelled = cancel_schedule(channel_id, schedule_id)
    if cancelled is None:
        raise HTTPException(404, "Schedule not found")
    await service.system_message(channel_id, f"{user.display_name} cancelled a schedule ({describe(cancelled)}): "
                                             f"{cancelled.prompt[:200]}")
    return {"ok": True}


@app.get("/api/channels/{channel_id}/agents/{agent_id}/memories")
async def list_memories(channel_id: int, agent_id: int, user: User = Depends(current_user),
                        session: Session = Depends(get_session)):
    _channel_or_404(session, channel_id, user)
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

"""Durable jobs shared by the Band bridge, API, and standalone worker."""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.models import AgentTask, db, utcnow


def serialize_task(task: AgentTask) -> dict:
    return task.model_dump(mode="json", exclude={"prompt", "context", "reply_to", "source_key", "result", "artifacts"})


def enqueue(*, agent_id: int, channel_id: int, room_id: str, source_id: str,
            prompt: str, context: str = "", reply_to: str = "") -> AgentTask:
    source_key = f"{agent_id}:{room_id}:{source_id}"
    with db() as session:
        existing = session.exec(select(AgentTask).where(AgentTask.source_key == source_key)).first()
        if existing:
            return existing
        task = AgentTask(id=uuid4().hex, source_key=source_key, agent_id=agent_id,
                         channel_id=channel_id, room_id=room_id, prompt=prompt,
                         context=context, reply_to=reply_to)
        session.add(task)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return session.exec(select(AgentTask).where(AgentTask.source_key == source_key)).one()
        session.refresh(task)
        return task


def update_task(task_id: str, **values) -> AgentTask:
    with db() as session:
        task = session.get(AgentTask, task_id)
        if task is None:
            raise LookupError(task_id)
        for key, value in values.items():
            setattr(task, key, value)
        task.updated_at = utcnow()
        session.add(task)
        session.commit()
        session.refresh(task)
        return task


def recover_tasks() -> None:
    """Call only while holding the exclusive worker lock."""
    with db() as session:
        for task in session.exec(select(AgentTask).where(AgentTask.status == "running")).all():
            task.status = "cancelled" if task.cancel_requested else "queued"
            task.progress = "Recovered after worker restart; resuming checkpoint"
            task.updated_at = utcnow()
            session.add(task)
        session.commit()


def next_task() -> AgentTask | None:
    # A filesystem lock guarantees one worker for this local deployment.
    with db() as session:
        task = session.exec(select(AgentTask).where(AgentTask.status == "queued")
                            .order_by(AgentTask.created_at)).first()
        if task is None:
            return None
        task.status = "running"
        task.progress = "Starting agent"
        task.attempts += 1
        task.updated_at = utcnow()
        session.add(task)
        session.commit()
        session.refresh(task)
        return task

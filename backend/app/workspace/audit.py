"""The audit log: an ordered record of what people and agents did, shown in a channel's Activity view."""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.persistence.models import AgentProfile, AgentTask, AuditEvent, db

SECRET_WORDS = ("apikey", "accesskey", "privatekey", "secret", "token", "passw", "authorization", "cookie",
                "credential", "signature", "bearer")
SECRET_VALUE = re.compile(r"\bbearer\s+[\w.~+/=-]+|\b(?:sk-|sk_|band_[a-z]_|ghp_|gho_|github_pat_|xox[abprs]-|AKIA)"
                          r"[\w-]{8,}", re.I)
SECRET_PARAM = re.compile(r"((?:api[_-]?key|access[_-]?token|token|secret|password)[\"']?\s*[=:]\s*[\"']?)[^\s&\"',}]+",
                          re.I)
MAX_VALUE = 200
MAX_ARGS = 800


def summarize(value: Any, limit: int = MAX_VALUE) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    text = SECRET_PARAM.sub(r"\1[hidden]", SECRET_VALUE.sub("[hidden]", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def summarize_args(args: Any) -> dict[str, str]:
    """A short view of tool arguments; values of secret-looking arguments are never stored."""
    if not isinstance(args, dict):
        return {"input": summarize(args)}
    summary: dict[str, str] = {}
    used = 0
    for name, value in args.items():
        if used >= MAX_ARGS:
            summary["…"] = f"{len(args) - len(summary)} more"
            break
        squashed = re.sub(r"[^a-z]", "", str(name).lower())
        summary[name] = "[hidden]" if any(word in squashed for word in SECRET_WORDS) else summarize(value)
        used += len(summary[name])
    return summary


def record(action: str, /, *, actor_type: str, actor_name: str, actor_id: int | None = None,
           channel_id: int | None = None, task_id: str | None = None, detail: dict[str, Any] | None = None,
           ref: str | None = None, session: Session | None = None) -> None:
    """Add an event. With `session`, it commits with the caller's transaction; `ref` makes it once per task."""
    event = AuditEvent(action=action, actor_type=actor_type, actor_name=actor_name[:100], actor_id=actor_id,
                       channel_id=channel_id, task_id=task_id, ref=ref,
                       detail={k: summarize(v) if isinstance(v, str) else v for k, v in (detail or {}).items()})
    if session is not None:
        session.add(event)
        return
    with db() as own:
        own.add(event)
        try:
            own.commit()
        except IntegrityError:
            own.rollback()


def record_agent(task: AgentTask, action: str, /, *, session: Session | None = None, ref: str | None = None,
                 **detail: Any) -> None:
    """Add an event done by the task's agent; see `record` for `session` and `ref`."""
    def add(target: Session) -> None:
        agent = target.get(AgentProfile, task.agent_id)
        record(action, actor_type="agent", actor_id=task.agent_id, actor_name=agent.name if agent else "Agent",
               channel_id=task.channel_id, task_id=task.id, detail=detail, ref=ref, session=target)

    if session is not None:
        add(session)
        return
    with db() as own:
        add(own)
        try:
            own.commit()
        except IntegrityError:
            own.rollback()


def serialize_event(event: AuditEvent) -> dict[str, Any]:
    return {"id": event.id, "at": event.at.isoformat(), "channel_id": event.channel_id, "task_id": event.task_id,
            "actor_type": event.actor_type, "actor_id": event.actor_id, "actor_name": event.actor_name,
            "action": event.action, "detail": event.detail}

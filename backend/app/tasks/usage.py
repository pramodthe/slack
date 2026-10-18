"""Token usage per model call and daily token budgets (UTC days) for agents and channels."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import settings
from app.persistence.models import AgentProfile, AgentTask, Budget, Channel, ChannelMember, TaskUsage, db, utcnow

logger = logging.getLogger(__name__)


def _prices() -> dict[str, dict[str, float]]:
    if not settings.model_prices:
        return {}
    try:
        prices = json.loads(settings.model_prices)
        return {model: {"input": float(rate.get("input", 0)), "output": float(rate.get("output", 0))}
                for model, rate in prices.items()}
    except (ValueError, AttributeError, TypeError):
        logger.warning("MODEL_PRICES is not valid JSON of per-model rates; costs are not shown")
        return {}


PRICES = _prices()


def cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Estimated USD from the configured rates, or None when the model has no rate."""
    rate = PRICES.get(model)
    if rate is None:
        return None
    return round((input_tokens * rate["input"] + output_tokens * rate["output"]) / 1_000_000, 4)


def record_usage(task: AgentTask, message, default_model: str) -> None:
    """Store one model response's token counts; the message id makes replays and re-streams count once."""
    usage = getattr(message, "usage_metadata", None)
    if not usage:
        return
    model = (getattr(message, "response_metadata", None) or {}).get("model_name") or default_model
    with db() as session:
        session.add(TaskUsage(task_id=task.id, channel_id=task.channel_id, agent_id=task.agent_id,
                              message_id=getattr(message, "id", None) or f"anonymous-{uuid4().hex}", model=model,
                              input_tokens=int(usage.get("input_tokens") or 0),
                              output_tokens=int(usage.get("output_tokens") or 0)))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()


def day_start(now: datetime | None = None) -> datetime:
    return (now or utcnow()).replace(hour=0, minute=0, second=0, microsecond=0)


def tokens_today(session: Session, *, agent_id: int | None = None, channel_id: int | None = None) -> int:
    query = select(func.coalesce(func.sum(TaskUsage.input_tokens + TaskUsage.output_tokens), 0)).where(
        TaskUsage.at >= day_start())
    if agent_id is not None:
        query = query.where(TaskUsage.agent_id == agent_id)
    if channel_id is not None:
        query = query.where(TaskUsage.channel_id == channel_id)
    return int(session.exec(query).one())


def over_budget(session: Session, agent_id: int, channel_id: int) -> str | None:
    """Why this agent can't work in this channel today, or None while both budgets have room."""
    for scope, scope_id in (("channel", channel_id), ("agent", agent_id)):
        budget = session.get(Budget, (scope, scope_id))
        if budget is None or tokens_today(session, **{f"{scope}_id": scope_id}) < budget.daily_tokens:
            continue
        if scope == "channel":
            channel = session.get(Channel, channel_id)
            owner = f"#{channel.name}" if channel else "This channel"
        else:
            agent = session.get(AgentProfile, agent_id)
            owner = agent.name if agent else "This agent"
        return f"{owner} used its daily budget of {budget.daily_tokens:,} tokens"
    return None


def task_usage(session: Session, task_ids: list[str]) -> dict[str, dict]:
    """Tokens (and estimated cost, when rates are configured) per task."""
    totals: dict[str, dict] = {}
    rows = session.exec(select(TaskUsage.task_id, TaskUsage.model, func.sum(TaskUsage.input_tokens),
                               func.sum(TaskUsage.output_tokens)).where(TaskUsage.task_id.in_(task_ids))
                        .group_by(TaskUsage.task_id, TaskUsage.model)).all()
    for task_id, model, input_tokens, output_tokens in rows:
        total = totals.setdefault(task_id, {"input_tokens": 0, "output_tokens": 0, "cost": None})
        total["input_tokens"] += int(input_tokens)
        total["output_tokens"] += int(output_tokens)
        if (price := cost(model, int(input_tokens), int(output_tokens))) is not None:
            total["cost"] = round((total["cost"] or 0) + price, 4)
    return totals


def channel_usage(session: Session, channel_id: int) -> dict:
    """Today's usage in a channel and per agent, with the budgets that apply."""
    since = day_start()
    rows = session.exec(select(TaskUsage.agent_id, TaskUsage.model, func.sum(TaskUsage.input_tokens),
                               func.sum(TaskUsage.output_tokens)).where(
        TaskUsage.channel_id == channel_id, TaskUsage.at >= since).group_by(TaskUsage.agent_id, TaskUsage.model)).all()
    members = session.exec(select(ChannelMember.member_id).where(ChannelMember.channel_id == channel_id,
                                                                 ChannelMember.member_type == "agent")).all()
    agents = {agent_id: {"agent_id": agent_id, "tokens": 0, "cost": None} for agent_id in members}
    for agent_id, model, input_tokens, output_tokens in rows:
        entry = agents.setdefault(agent_id, {"agent_id": agent_id, "tokens": 0, "cost": None})
        entry["tokens"] += int(input_tokens) + int(output_tokens)
        if (price := cost(model, int(input_tokens), int(output_tokens))) is not None:
            entry["cost"] = round((entry["cost"] or 0) + price, 4)
    for entry in agents.values():
        budget = session.get(Budget, ("agent", entry["agent_id"]))
        entry["agent_tokens_today"] = tokens_today(session, agent_id=entry["agent_id"])
        entry["agent_budget"] = budget.daily_tokens if budget else None
    channel_budget = session.get(Budget, ("channel", channel_id))
    costs = [a["cost"] for a in agents.values() if a["cost"] is not None]
    return {"since": since.isoformat(), "tokens": sum(a["tokens"] for a in agents.values()),
            "cost": round(sum(costs), 4) if costs else None,
            "budget": channel_budget.daily_tokens if channel_budget else None,
            "agents": sorted(agents.values(), key=lambda a: a["tokens"], reverse=True),
            "prices_configured": bool(PRICES)}

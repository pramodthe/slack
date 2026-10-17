"""Durable jobs shared by the Band bridge, API, and standalone worker."""
from __future__ import annotations

import re
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.persistence.models import (AgentProfile, AgentTask, ChannelMember, DirectMessage, Message, OutsideHandoff,
                                    TaskApproval, TaskHandoff, db, utcnow)

# Teammate hand-offs (local or outside) allowed per original request; each also yields at most one report back.
MAX_HANDOFFS = 6


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
        # An outside request interrupted mid-send may or may not have reached Band; it is never resent.
        session.exec(update(OutsideHandoff).where(OutsideHandoff.status == "sending").values(status="unknown"))
        session.commit()


def handoff_root(session, task_id: str) -> str:
    """The original request a task works on behalf of."""
    link = session.get(TaskHandoff, task_id)
    return link.root_task_id if link else task_id


def handoffs_used(session, root: str) -> int:
    """Local and outside hand-offs made for one original request, so neither kind can loop forever."""
    chain = [root, *session.exec(select(TaskHandoff.task_id).where(TaskHandoff.root_task_id == root)).all()]
    local = session.exec(select(TaskHandoff.task_id).where(
        TaskHandoff.root_task_id == root, TaskHandoff.kind == "handoff")).all()
    outside = session.exec(select(OutsideHandoff.source_key).where(OutsideHandoff.task_id.in_(chain))).all()
    return len(local) + len(outside)


def next_task() -> AgentTask | None:
    """Claim the oldest queued task whose agent is not already running in that channel.

    Keeping each agent sequential per channel preserves conversational order; other agents
    and channels run in parallel. A filesystem lock guarantees one worker for this deployment.
    """
    with db() as session:
        busy = {(t.agent_id, t.channel_id) for t in session.exec(
            select(AgentTask).where(AgentTask.status == "running")).all()}
        task = next((t for t in session.exec(select(AgentTask).where(AgentTask.status == "queued")
                     .order_by(AgentTask.created_at)).all() if (t.agent_id, t.channel_id) not in busy), None)
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


def display_names(session, text: str) -> str:
    """Replace agents' Band handles and raw mentions (@owner/master, @[[id]]) with display names, so agents reply with @Master."""
    for agent in session.exec(select(AgentProfile)).all():
        text = text.replace(f"@[[{agent.band_agent_id}]]", f"@{agent.name}")
        if agent.handle:
            text = re.sub(rf"@{re.escape(agent.handle)}(?![\w./-])", lambda _: f"@{agent.name}", text, flags=re.I)
    return text


def channel_context(session, channel_id: int) -> str:
    """Recent channel messages, bounded like Band turns; the harness treats them as untrusted."""
    recent = session.exec(select(Message).where(Message.channel_id == channel_id)
                          .order_by(Message.id.desc()).limit(30)).all()
    return display_names(session, "\n".join(f"{m.author_name}: {m.content}" for m in reversed(recent))[-24000:])


def hand_off(task: AgentTask, *, teammate: str, request: str, report_back: bool, call_id: str) -> dict:
    """Post a visible request to a teammate and queue their task atomically.

    The tool call id makes a replayed call (after a worker crash) return the original hand-off.
    """
    request = request.strip()
    if not request or len(request) > 8000:
        return {"error": "Provide a request of 1-8000 characters."}
    with db() as session:
        if session.get(DirectMessage, task.channel_id):
            return {"error": "This is a private one-on-one conversation, so teammates can't join it. "
                             "Use internal helpers, or suggest the person ask the teammate in a shared channel."}
        sender = session.get(AgentProfile, task.agent_id)
        others = [a for a in session.exec(select(AgentProfile)).all() if a.id != task.agent_id]
        wanted = teammate.strip().lstrip("@").lower()
        target = next((a for a in others if wanted in (a.name.lower(), (a.handle or "").lower())), None)
        if sender is None or target is None:
            names = ", ".join(a.name for a in others) or "none"
            return {"error": f"No teammate named {teammate!r}. Available teammates: {names}. "
                             "Use internal helpers if none fit."}
        source_key = f"{target.id}:{task.room_id}:handoff-{call_id}"
        existing = session.exec(select(AgentTask).where(AgentTask.source_key == source_key)).first()
        if existing:
            return {"status": "handed_off", "teammate": target.name, "task_id": existing.id}
        root = handoff_root(session, task.id)
        if handoffs_used(session, root) >= MAX_HANDOFFS:
            return {"error": f"This request already used {MAX_HANDOFFS} teammate hand-offs. "
                             "Finish the work yourself and answer the humans."}
        child = AgentTask(id=uuid4().hex, source_key=source_key, channel_id=task.channel_id,
                          agent_id=target.id, room_id=task.room_id, prompt=f"**{sender.name}:** {request}",
                          context=channel_context(session, task.channel_id),
                          # A report-back answer goes to the requester, so it stays local instead of pinging Band.
                          reply_to="" if report_back else task.reply_to)
        session.add_all([
            Message(channel_id=task.channel_id, author_type="agent", author_id=sender.id,
                    author_name=sender.name, content=f"@{target.name} {request}", to_humans=False,
                    mentions=[{"type": "agent", "id": target.id, "name": target.name}],
                    band_message_id=f"handoff:{call_id}"),
            child,
            TaskHandoff(task_id=child.id, root_task_id=root, parent_task_id=task.id,
                        from_agent_id=sender.id, kind="handoff", request=request, report_back=report_back),
        ])
        if not session.get(ChannelMember, (task.channel_id, "agent", target.id)):
            session.add(ChannelMember(channel_id=task.channel_id, member_type="agent", member_id=target.id))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.exec(select(AgentTask).where(AgentTask.source_key == source_key)).one()
            return {"status": "handed_off", "teammate": target.name, "task_id": existing.id}
    return {"status": "handed_off", "teammate": target.name, "task_id": child.id,
            "note": (f"{target.name} runs after your turn ends. " + (
                "You will get a new task with their answer. End your turn now with a one-line status."
                if report_back else f"{target.name} will answer the humans directly."))}


def report_target(session, task: AgentTask) -> tuple[TaskHandoff, AgentProfile] | None:
    """The requester waiting for this task's answer, if it was a report-back hand-off."""
    link = session.get(TaskHandoff, task.id)
    if link is None or link.kind != "handoff" or not link.report_back:
        return None
    requester = session.get(AgentProfile, link.from_agent_id)
    return (link, requester) if requester else None


def queue_report(session, task: AgentTask, link: TaskHandoff, teammate_name: str, result: str) -> None:
    """Called inside the completion transaction so an answer is reported back exactly once."""
    source_key = f"{link.from_agent_id}:{task.room_id}:report-{task.id}"
    if session.exec(select(AgentTask.id).where(AgentTask.source_key == source_key)).first():
        return
    root = session.get(AgentTask, link.root_task_id)
    parent = session.get(AgentTask, link.parent_task_id)
    # Sibling hand-offs from the same turn may still be running in parallel.
    waiting = session.exec(select(AgentProfile.name).join(AgentTask, AgentTask.agent_id == AgentProfile.id)
                           .join(TaskHandoff, TaskHandoff.task_id == AgentTask.id).where(
        TaskHandoff.parent_task_id == link.parent_task_id, TaskHandoff.kind == "handoff",
        AgentTask.id != task.id, AgentTask.status.in_(["queued", "running"]))).all()
    prompt = (f"{teammate_name} finished the work you handed off.\n\nYou asked:\n{link.request}\n\n"
              f"{teammate_name} answered:\n{result[:20000]}\n\n"
              + (f"The original request was:\n{root.prompt}\n\n" if root else "")
              + (f"Still working on your other hand-offs: {', '.join(sorted(set(waiting)))}. If you need their "
                 "results, reply with a one-line status; it is shown as agent chatter, not sent to the humans. "
                 "Each answer arrives as a new task. Earlier answers are in the room context.\n\n" if waiting else "")
              + "Continue the original request: combine the results, hand off the next step, "
                "or give the humans the final answer.")
    # While siblings are still working, the requester's reply is an interim status: local chatter, not Band.
    follow_up = AgentTask(id=uuid4().hex, source_key=source_key, channel_id=task.channel_id,
                          agent_id=link.from_agent_id, room_id=task.room_id, prompt=prompt,
                          context=channel_context(session, task.channel_id),
                          reply_to="" if waiting or parent is None else parent.reply_to)
    session.add_all([follow_up, TaskHandoff(task_id=follow_up.id, root_task_id=link.root_task_id,
                                            parent_task_id=task.id, from_agent_id=task.agent_id,
                                            kind="interim" if waiting else "report", report_back=False)])


def is_interim(session, task: AgentTask) -> bool:
    link = session.get(TaskHandoff, task.id)
    return link is not None and link.kind == "interim"


def await_approval(task: AgentTask, requests: list[tuple[str, dict]]) -> None:
    """Record paused actions, tell the channel, and park the task until every request is decided."""
    with db() as session:
        profile = session.get(AgentProfile, task.agent_id)
        for approval_id, value in requests:
            if session.get(TaskApproval, approval_id):
                continue
            action = str(value.get("action") or "Unspecified action")[:500]
            details = str(value.get("details") or "")[:4000]
            session.add(TaskApproval(id=approval_id, task_id=task.id, channel_id=task.channel_id,
                                     agent_id=task.agent_id, action=action, details=details))
            session.add(Message(channel_id=task.channel_id, author_type="agent", author_id=task.agent_id,
                                author_name=profile.name if profile else "Agent", to_humans=True,
                                content=f"**Approval needed:** {action}" + (f"\n\n{details}" if details else "")
                                        + "\n\nApprove or deny it in the Agent tasks panel.",
                                band_message_id=f"approval:{approval_id}"))
        # A cancellation requested while the task ran wins over waiting.
        session.exec(update(AgentTask).where(AgentTask.id == task.id, AgentTask.status == "running",
                                             AgentTask.cancel_requested == True)  # noqa: E712
                     .values(status="cancelled", progress="Cancelled", updated_at=utcnow()))
        session.exec(update(AgentTask).where(AgentTask.id == task.id, AgentTask.status == "running")
                     .values(status="waiting", progress="Waiting for approval", updated_at=utcnow()))
        session.commit()


def approval_decisions(interrupt_ids: list[str]) -> dict[str, dict] | None:
    """Resume values keyed by interrupt id, or None while any request is undecided."""
    with db() as session:
        rows = [session.get(TaskApproval, approval_id) for approval_id in interrupt_ids]
    if any(row is None or row.status == "pending" for row in rows):
        return None
    return {row.id: {"approved": row.status == "approved", "decided_by": row.decided_by, "note": row.note}
            for row in rows}


def decide_approval(approval_id: str, *, approve: bool, decided_by: str, note: str = "") -> TaskApproval | None:
    """Apply a decision once; requeue the task when its last pending request is decided.

    Only waiting tasks accept decisions, so a cancelled task's request cannot be approved.
    """
    with db() as session:
        approval = session.get(TaskApproval, approval_id)
        task = session.get(AgentTask, approval.task_id) if approval else None
        if task is None or task.status != "waiting":
            return None
        claimed = session.exec(update(TaskApproval).where(
            TaskApproval.id == approval_id, TaskApproval.status == "pending").values(
            status="approved" if approve else "denied", decided_by=decided_by, note=note[:2000],
            decided_at=utcnow()))
        if not claimed.rowcount:
            session.rollback()
            return None
        approval = session.get(TaskApproval, approval_id)
        if not session.exec(select(TaskApproval.id).where(TaskApproval.task_id == approval.task_id,
                                                          TaskApproval.status == "pending")).first():
            session.exec(update(AgentTask).where(AgentTask.id == approval.task_id, AgentTask.status == "waiting")
                         .values(status="queued", progress="Decision received; resuming", updated_at=utcnow()))
        session.commit()
        session.refresh(approval)
        return approval

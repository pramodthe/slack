"""Run separately: uv run python -m app.worker (one worker per checkpoint file)."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from band.client.rest import AsyncRestClient, ChatMessageRequest, ChatMessageRequestMentionsItem, aclose_rest_client
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from deepagents.backends.utils import file_data_to_string
from sqlmodel import select
from sqlalchemy import update

from app.auth import decrypt_secret
from app.config import settings
from app.harness import build_harness
from app.computer import TaskComputer
from app.models import AgentProfile, AgentTask, Message, db, init_db, utcnow
from app.tasks import next_task, recover_tasks, update_task

logger = logging.getLogger(__name__)


def config_for(task: AgentTask) -> dict:
    return {"configurable": {"thread_id": task.id}, "recursion_limit": settings.task_recursion_limit}


async def execute(task: AgentTask, checkpointer, *, factory=build_harness, computer: TaskComputer | None = None) -> str:
    async with (computer or TaskComputer(task)) as computer:
        return await _execute_on_computer(task, checkpointer, factory=factory, computer=computer)


async def _execute_on_computer(task: AgentTask, checkpointer, *, factory, computer) -> str:
    with db() as session:
        profile = session.get(AgentProfile, task.agent_id)
    if profile is None:
        raise ValueError("Agent was deleted before this task could finish")
    graph = factory(task, profile, checkpointer, computer=computer)
    config = config_for(task)
    snapshot = await graph.aget_state(config)
    if snapshot.values and not snapshot.next:
        final = snapshot.values
    else:
        inputs = None if snapshot.values else {"messages": [{"role": "user", "content":
            f"Recent room context:\n{task.context}\n\nRequest:\n{task.prompt}"}]}
        async for _namespace, update in graph.astream(inputs, config, stream_mode="updates",
                                                     subgraphs=True, durability="sync"):
            labels = []
            for _node, value in update.items():
                if not isinstance(value, dict):
                    continue
                if value.get("todos"):
                    labels.extend(f"{t['status']}: {t['content']}" for t in value["todos"])
                for message in value.get("messages", []):
                    for call in getattr(message, "tool_calls", []):
                        detail = call["args"].get("subagent_type", "") if call["name"] == "task" else ""
                        labels.append(f"Using {call['name']} {detail}".strip())
                    if getattr(message, "type", "") == "tool":
                        labels.append(f"Finished {getattr(message, 'name', None) or 'tool'}")
            if labels:
                update_task(task.id, progress=" · ".join(labels)[:1000])
        final = (await graph.aget_state(config)).values
    with db() as session:
        exports = session.get(AgentTask, task.id).artifacts
    combined = {a["path"]: a["content"] for a in exports}
    for path, data in final.get("files", {}).items():
        if not isinstance(data, dict) or "content" not in data:
            continue
        combined.setdefault(path, file_data_to_string(data))
    artifacts = []
    remaining = 200000
    for path, content in combined.items():
        content = content[:remaining]
        artifacts.append({"path": path, "content": content})
        remaining -= len(content)
        if remaining <= 0 or len(artifacts) >= 20:
            break
    update_task(task.id, artifacts=artifacts)
    for message in reversed(final.get("messages", [])):
        if getattr(message, "type", "") == "ai" and not getattr(message, "tool_calls", []):
            text = message.text
            if text.strip():
                return text[:60000]
    raise ValueError("Agent finished without a final answer")


def complete(task: AgentTask, result: str) -> None:
    # Result and local message are committed together. Replaying completion cannot duplicate a reply.
    with db() as session:
        claimed = session.exec(update(AgentTask).where(
            AgentTask.id == task.id, AgentTask.status == "running", AgentTask.cancel_requested == False
        ).values(result=result, status="completed", progress="Completed", updated_at=utcnow()))
        if claimed.rowcount:
            profile = session.get(AgentProfile, task.agent_id)
            if profile is None:
                raise ValueError("Agent was deleted")
            key = f"task:{task.id}"
            if not session.exec(select(Message).where(Message.band_message_id == key)).first():
                message = Message(channel_id=task.channel_id, author_type="agent", author_id=task.agent_id,
                                  author_name=profile.name, content=result, band_message_id=key, to_humans=True)
                session.add(message)
                session.flush()
                session.exec(update(AgentTask).where(AgentTask.id == task.id).values(reply_message_id=message.id))
        else:
            session.exec(update(AgentTask).where(AgentTask.id == task.id, AgentTask.status == "running",
                         AgentTask.cancel_requested == True).values(status="cancelled", progress="Cancelled"))
        session.commit()


async def deliver(task: AgentTask) -> None:
    """At-most-once send attempt; ambiguous sends require human review, never blind retry."""
    if not task.reply_to:
        update_task(task.id, delivery_status="local_only")
        return
    with db() as session:
        profile = session.get(AgentProfile, task.agent_id)
    if profile is None:
        update_task(task.id, delivery_status="agent_deleted")
        return
    with db() as session:
        claimed = session.exec(update(AgentTask).where(AgentTask.id == task.id,
            AgentTask.status == "completed", AgentTask.delivery_status == "pending")
            .values(delivery_status="sending", updated_at=utcnow()))
        session.commit()
        if not claimed.rowcount:
            return
    client = None
    try:
        client = AsyncRestClient(api_key=decrypt_secret(profile.band_api_key_enc), base_url=settings.band_rest_url)
        response = await client.agent_api_messages.create_agent_chat_message(
            task.room_id, message=ChatMessageRequest(content=task.result[:8000], mentions=[
                ChatMessageRequestMentionsItem(id=task.reply_to)]), request_options={"max_retries": 0, "timeout_in_seconds": 30})
        with db() as session:
            message = session.exec(select(Message).where(Message.band_message_id == f"task:{task.id}")).first()
            if message:
                message.band_message_id = response.data.id
                session.add(message)
                session.commit()
        update_task(task.id, delivery_status="sent")
    except Exception:
        logger.exception("Band delivery outcome uncertain for task %s; local answer retained", task.id)
        update_task(task.id, delivery_status="unknown")
    finally:
        if client is not None:
            await aclose_rest_client(client)


async def process(task: AgentTask, saver, *, executor=execute) -> None:
    runner = asyncio.create_task(executor(task, saver))
    try:
        async with asyncio.timeout(settings.task_timeout):
            while not runner.done():
                with db() as session:
                    current = session.get(AgentTask, task.id)
                if current.cancel_requested:
                    runner.cancel()
                    await asyncio.gather(runner, return_exceptions=True)
                    update_task(task.id, status="cancelled", progress="Cancelled")
                    return
                await asyncio.wait({runner}, timeout=0.5)
            result = await runner
            complete(task, result)
    except asyncio.CancelledError:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        # Leave running state for restart recovery; never turn shutdown into task failure.
        raise
    except Exception as exc:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        # Keep raw provider errors out of the UI (they can include credentials/headers).
        logger.exception("Task %s failed", task.id)
        update_task(task.id, status="failed", progress="Task failed; retry to resume saved progress",
                    error="Time limit reached" if isinstance(exc, TimeoutError) else type(exc).__name__)


def _lock_exclusively(lock) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("A worker already owns this checkpoint file")


async def work() -> None:
    # A supervisor's SIGTERM gets the same cancellation/cleanup path as Ctrl+C.
    # Windows event loops don't support signal handlers; Ctrl+C still works there.
    if sys.platform != "win32":
        current = asyncio.current_task()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, current.cancel)
    init_db()
    path = Path(settings.checkpoint_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".worker.lock", "w") as lock:
        _lock_exclusively(lock)
        recover_tasks()
        # A crash between send and receipt cannot safely be retried.
        with db() as session:
            for task in session.exec(select(AgentTask).where(AgentTask.delivery_status == "sending")).all():
                task.delivery_status = "unknown"
                task.updated_at = utcnow()
                session.add(task)
            session.commit()
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            await saver.setup()
            logger.info("Deep Agents worker ready")
            while True:
                task = next_task()
                if task:
                    await process(task, saver)
                with db() as session:
                    pending = session.exec(select(AgentTask).where(
                        AgentTask.status == "completed", AgentTask.delivery_status == "pending")).all()
                for item in pending:
                    await deliver(item)
                if not task:
                    await asyncio.sleep(0.5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(work())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

"""Reply drafts: the parent agent's answer as it is written, shown in the channel until the final message lands."""
from __future__ import annotations

import time

from sqlalchemy import delete

from app.persistence.models import AgentTask, MessageDraft, db, utcnow

DRAFT_INTERVAL = 0.3
MAX_DRAFT = 60000


NOT_TOOLS = (None, "text", "thinking", "redacted_thinking")


def _after_tools(content) -> str | None:
    """Text after the last server-side tool block (e.g. code execution), or None when there is none."""
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    tools = [i for i, b in enumerate(blocks) if b.get("type") not in NOT_TOOLS]
    if not tools:
        return None
    return "".join(b.get("text", "") for b in blocks[tools[-1] + 1:] if b.get("type") == "text")


def answer_text(message) -> str:
    """The reply without text the model wrote before running server-side tools."""
    after = _after_tools(getattr(message, "content", ""))
    return after if after and after.strip() else message.text


def save_draft(task: AgentTask, content: str) -> None:
    with db() as session:
        if content.strip():
            session.merge(MessageDraft(task_id=task.id, channel_id=task.channel_id, agent_id=task.agent_id,
                                       content=content[:MAX_DRAFT], updated_at=utcnow()))
        else:
            session.exec(delete(MessageDraft).where(MessageDraft.task_id == task.id))
        session.commit()


def clear_draft(task_id: str | None = None) -> None:
    """Remove one task's draft, or every draft when a worker starts (none can still be in progress)."""
    with db() as session:
        query = delete(MessageDraft)
        session.exec(query if task_id is None else query.where(MessageDraft.task_id == task_id))
        session.commit()


class Draft:
    """Accumulates the root agent's model tokens; tool activity resets it so only the final answer remains."""

    def __init__(self, task: AgentTask, enabled: bool):
        self.task = task
        self.enabled = enabled
        self.text = ""
        self.saved = ""
        self.saved_at = 0.0

    def feed(self, namespace: tuple, chunk, metadata: dict) -> None:
        if not self.enabled or namespace:
            return
        kind = getattr(chunk, "type", "")
        if kind == "tool":
            self.reset()
        elif kind in ("ai", "AIMessageChunk") and metadata.get("langgraph_node") == "model":
            if getattr(chunk, "tool_call_chunks", None) or getattr(chunk, "tool_calls", None):
                self.reset()
            elif (after := _after_tools(chunk.content)) is not None:
                self.text = after
                self.flush(force=True)
            else:
                self.text += chunk.text
                self.flush()

    def reset(self) -> None:
        self.text = ""
        self.flush(force=True)

    def flush(self, force: bool = False) -> None:
        if not self.enabled or self.text == self.saved:
            return
        if not force and time.monotonic() - self.saved_at < DRAFT_INTERVAL:
            return
        save_draft(self.task, self.text)
        self.saved = self.text
        self.saved_at = time.monotonic()

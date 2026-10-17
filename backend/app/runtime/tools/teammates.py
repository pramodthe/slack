"""Hand work to another visible agent in the channel."""
from __future__ import annotations

from typing import Annotated

from langchain_core.tools import InjectedToolCallId, tool

from app.persistence.models import AgentTask
from app.tasks.queue import hand_off


def teammate_tools(task: AgentTask):
    @tool
    def ask_teammate(teammate: str, request: str, tool_call_id: Annotated[str, InjectedToolCallId],
                     report_back: bool = True) -> dict:
        """Hand work to a visible teammate agent by name. Posts your request in the channel for everyone to see.

        The teammate runs after your turn ends, with their own role and memory. With report_back (default),
        their answer comes back to you as a new task so you can continue; end your turn with a short status.
        With report_back=False, the teammate answers the humans directly. Give complete context and the
        expected output. Use internal helpers (the task tool) instead for quick private subtasks.
        """
        return hand_off(task, teammate=teammate, request=request, report_back=report_back, call_id=tool_call_id)

    return [ask_teammate]

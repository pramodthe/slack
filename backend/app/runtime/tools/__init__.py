"""Tools passed to each task's Deep Agent. Order matches the parent tool list."""
from __future__ import annotations

from app.computer import TaskComputer
from app.persistence.models import AgentTask
from app.runtime.tools.approvals import approval_tools
from app.runtime.tools.channel import channel_tools
from app.runtime.tools.computer import computer_tools
from app.runtime.tools.memory import memory_tools
from app.runtime.tools.schedules import schedule_tools
from app.runtime.tools.teammates import teammate_tools
from app.runtime.tools.web import web_tools


def tools_for(task: AgentTask, computer: TaskComputer | None = None):
    computer = computer or TaskComputer(task)
    return [
        *memory_tools(task),
        *channel_tools(task),
        *web_tools(task),
        *teammate_tools(task),
        *approval_tools(task),
        *schedule_tools(task),
        *computer_tools(task, computer),
    ]

"""Daytona desktop, shell, and file tools for one task."""
from __future__ import annotations

import logging
from typing import Annotated, Literal

from langchain_core.tools import tool
from pydantic import Field

from app.computer import ComputerUnavailable, TaskComputer
from app.persistence.models import AgentTask

logger = logging.getLogger(__name__)


def computer_tools(task: AgentTask, computer: TaskComputer):
    async def computer_call(operation):
        try:
            result = await operation()
            if computer.notice:
                # Surface a replaced computer once, alongside the result that triggered it.
                notice, computer.notice = computer.notice, ""
                if isinstance(result, list):
                    return [{"type": "text", "text": notice}, *result]
                return {**result, "notice": notice}
            return result
        except ComputerUnavailable as exc:
            return {"error": str(exc)}
        except Exception as exc:
            # SDK errors may contain full request headers. Expose only the type.
            logger.warning("Daytona tool failed for task %s: %s", task.id, type(exc).__name__)
            return {"error": f"Daytona operation failed ({type(exc).__name__}). Its outcome may be uncertain. "
                             "For desktop actions, take a fresh screenshot before deciding what to do next."}

    @tool
    async def run_python(code: str) -> dict:
        """Run Python on your Daytona computer for this channel (30s limit). Never executes on the host.

        Files persist across calls and later tasks; Python variables do not. Requires DAYTONA_API_KEY.
        """
        return await computer_call(lambda: computer.command(code, python=True))

    @tool
    async def sandbox_exec(command: str) -> dict:
        """Run a shell command inside the task's Daytona sandbox, with bounded output and a 30s limit.

        Use for installing packages, running tests, or inspecting sandbox files. Host secrets are not available.
        Long commands should start a background process with redirected output, then poll its log.
        """
        return await computer_call(lambda: computer.command(command))

    @tool
    async def computer_screenshot() -> list | dict:
        """See the Daytona desktop. Returns an actual image and screen_id required by computer_action.

        Use the image to choose coordinates. Never guess from an old screenshot. Starts a computer on first use.
        """
        return await computer_call(computer.screenshot)

    @tool
    async def computer_open_browser(url: str) -> list | dict:
        """Open an HTTP(S) URL in the sandbox's graphical browser and see its desktop screenshot.

        Requires a desktop snapshot with Chromium or Firefox. This uses the cloud computer, not a local browser.
        """
        return await computer_call(lambda: computer.open_browser(url))

    @tool
    async def computer_action(
        screen_id: str, action: Literal["click", "move", "drag", "scroll", "type", "key", "wait"],
        x: Annotated[int, Field(ge=0)] = 0, y: Annotated[int, Field(ge=0)] = 0,
        end_x: Annotated[int, Field(ge=0)] = 0, end_y: Annotated[int, Field(ge=0)] = 0,
        text: Annotated[str, Field(max_length=4000)] = "", key: Annotated[str, Field(max_length=40)] = "",
        modifiers: list[Literal["ctrl", "alt", "shift", "cmd"]] | None = None,
        button: Literal["left", "right", "middle"] = "left", double: bool = False,
        direction: Literal["up", "down"] = "down", amount: Annotated[int, Field(ge=1, le=20)] = 3,
        seconds: Annotated[float, Field(ge=0.1, le=3)] = 1,
    ) -> list | dict:
        """Operate the remote desktop using a fresh screen_id, then observe the resulting screenshot.

        click/move/scroll use x,y; drag also uses end_x,end_y; type uses text; key uses key and modifiers
        (e.g. key='l', modifiers=['ctrl']). wait uses seconds. Each screen_id is valid for one action.
        On an uncertain failure, take a new screenshot instead of repeating an old action.
        """
        return await computer_call(lambda: computer.action(screen_id=screen_id, action=action, x=x, y=y,
            end_x=end_x, end_y=end_y, text=text, key=key, modifiers=modifiers, button=button, double=double,
            direction=direction, amount=amount, seconds=seconds))

    @tool
    async def sandbox_write_file(path: str, content: str) -> dict:
        """Write UTF-8 text to an absolute path in the Daytona computer (up to 200000 bytes).

        Deep Agents virtual files are separate. Use this tool to copy code or data onto the actual computer.
        """
        return await computer_call(lambda: computer.write_file(path, content))

    @tool
    async def sandbox_export_file(path: str) -> dict:
        """Export a UTF-8 text file from the Daytona computer to downloadable task results (up to 200000 bytes).

        Export final deliverables before finishing. Stopped sandboxes retain files until their auto-delete timer.
        """
        return await computer_call(lambda: computer.export_file(path))

    return [run_python, sandbox_exec, computer_screenshot, computer_open_browser, computer_action,
            sandbox_write_file, sandbox_export_file]

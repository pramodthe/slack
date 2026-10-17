"""Tools Anthropic runs on its side during the model call; they never touch the agent's Daytona computer."""
from __future__ import annotations

from app.config import settings

CODE_EXECUTION = {"type": "code_execution_20260521", "name": "code_execution"}


def hosted_tools(model_name: str) -> list[dict]:
    # The Claude 3 family predates this code execution tool version.
    if not settings.fast_code_execution or not model_name.startswith("claude-") or model_name.startswith("claude-3"):
        return []
    return [CODE_EXECUTION]

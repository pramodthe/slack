"""Code and calculations on the task computer."""
from __future__ import annotations

from langchain.agents.middleware import ModelCallLimitMiddleware


def coder(by_name: dict) -> dict:
    return {
        "name": "coder",
        "description": "Develop code and verify calculations or tests in the isolated Python sandbox.",
        "system_prompt": "Write minimal correct code. Use run_python to verify when available. Report execution limits honestly.",
        "middleware": [ModelCallLimitMiddleware(run_limit=12, exit_behavior="error")],
        "tools": [by_name[n] for n in ("run_python", "sandbox_exec", "sandbox_write_file", "sandbox_export_file",
                                       "request_approval", "channel_history", "recall_memory")],
    }

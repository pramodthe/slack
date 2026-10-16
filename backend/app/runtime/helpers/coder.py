"""Code and calculations on the task computer."""
from __future__ import annotations

from langchain.agents.middleware import ModelCallLimitMiddleware


def coder(by_name: dict) -> dict:
    return {
        "name": "coder",
        "description": "Develop code and verify calculations or tests in the isolated Python sandbox.",
        "system_prompt": (
            "Write minimal correct code. Use code_execution, when available, for quick self-contained calculations "
            "and data work: it starts instantly but cannot see files on the agent's computer. Use run_python when "
            "the work needs those files, installed packages, or the network. Report execution limits honestly."),
        "middleware": [ModelCallLimitMiddleware(run_limit=12, exit_behavior="error")],
        "tools": [by_name[n] for n in ("code_execution", "run_python", "sandbox_exec", "sandbox_write_file",
                                       "sandbox_export_file", "request_approval", "channel_history", "recall_memory")
                  if n in by_name],
    }

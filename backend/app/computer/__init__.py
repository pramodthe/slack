"""Daytona desktop for one agent in one channel."""
from app.computer.desktop import (
    CONTROL_MINUTES,
    TRANSFER_LIMIT,
    VNC_PORT,
    ComputerUnavailable,
    TaskComputer,
    active_control,
    computer_key,
    computer_owner,
    is_quota_error,
    release_control,
    sandbox_record,
    save_sandbox,
    take_control,
    workspace_id,
)

__all__ = [
    "CONTROL_MINUTES",
    "TRANSFER_LIMIT",
    "VNC_PORT",
    "ComputerUnavailable",
    "TaskComputer",
    "active_control",
    "computer_key",
    "computer_owner",
    "is_quota_error",
    "release_control",
    "sandbox_record",
    "save_sandbox",
    "take_control",
    "workspace_id",
]

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT_DIR / "backend"
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"

load_dotenv(ROOT_DIR / ".env")
load_dotenv(BACKEND_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str = _env("ANTHROPIC_API_KEY")
    band_user_api_key: str = _env("BAND_USER_API_KEY")
    master_agent_name: str = _env("MASTER_AGENT_NAME", "Master")
    band_rest_url: str = _env("BAND_REST_URL", "https://app.band.ai")
    band_ws_url: str = _env("BAND_WS_URL", "wss://app.band.ai/api/v1/socket/websocket")
    app_secret: str = _env("APP_SECRET", "dev-secret-change-me")
    database_url: str = _env("DATABASE_URL", f"sqlite:///{(BACKEND_DIR / 'band_slack.db').as_posix()}")
    default_model: str = _env("DEFAULT_MODEL", "claude-sonnet-5-5")
    checkpoint_path: str = _env("AGENT_CHECKPOINT_PATH", str(BACKEND_DIR / "agent_checkpoints.db"))
    task_timeout: float = float(_env("AGENT_TASK_TIMEOUT", "900"))
    task_recursion_limit: int = int(_env("AGENT_RECURSION_LIMIT", "100"))
    worker_concurrency: int = max(1, int(_env("WORKER_CONCURRENCY", "4")))
    default_timezone: str = _env("DEFAULT_TIMEZONE", "UTC")
    mcp_config_path: str = _env("MCP_CONFIG_PATH", str(BACKEND_DIR / "mcp_servers.json"))
    max_agent_teammates: int = int(_env("MAX_AGENT_TEAMMATES", "10"))
    fast_code_execution: bool = _env("FAST_CODE_EXECUTION", "on").lower() not in ("0", "off", "false", "no")
    exa_api_key: str = _env("EXA_API_KEY")
    daytona_api_key: str = _env("DAYTONA_API_KEY")
    daytona_api_url: str = _env("DAYTONA_API_URL", "https://app.daytona.io/api")
    daytona_target: str = _env("DAYTONA_TARGET")
    daytona_snapshot: str = _env("DAYTONA_SNAPSHOT")
    daytona_auto_stop: int = int(_env("DAYTONA_AUTO_STOP_MINUTES", "15"))
    daytona_auto_delete: int = int(_env("DAYTONA_AUTO_DELETE_MINUTES", "10080"))
    poll_seconds: float = float(_env("BAND_POLL_SECONDS", "5"))
    # JSON of USD per million tokens, e.g. {"claude-sonnet-5-5": {"input": 3, "output": 15}}; empty shows no cost.
    model_prices: str = _env("MODEL_PRICES")


settings = Settings()

MODELS = [
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5 (fast)"},
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5 (balanced)"},
    {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8 (most capable)"},
]

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

from band import Agent
from band.adapters.anthropic import AnthropicAdapter, AnthropicAdapterConfig
from band.client.rest import DEFAULT_REQUEST_OPTIONS, AsyncRestClient, aclose_rest_client
from band.core.types import Emit
from band.runtime.custom_tools import CustomToolDef
from band.runtime.tools.types import BandTool

from app.auth import decrypt_secret
from app.config import settings
from app.models import AgentProfile
from app.roles import build_prompt

logger = logging.getLogger(__name__)

current_room: ContextVar[str | None] = ContextVar("current_room", default=None)


class AgentSink(Protocol):
    async def agent_message(
        self,
        profile_id: int,
        room_id: str,
        content: str,
        mentions: list[str],
        band_message_id: str | None,
    ) -> None: ...

    async def agent_typing(self, profile_id: int, room_id: str, active: bool) -> None: ...

    async def agent_status(self, profile_id: int, state: str, error: str | None) -> None: ...


def describe_error(exc: BaseException) -> str:
    """Band REST errors stringify with full response headers; keep just the message."""
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else getattr(body, "error", None)
    message = error.get("message") if isinstance(error, dict) else getattr(error, "message", None)
    status = getattr(exc, "status_code", None)
    if message:
        return f"{message} (HTTP {status})" if status else str(message)
    if status:
        return f"Band returned HTTP {status}"
    return str(exc) or type(exc).__name__


def _message_id(result: Any) -> str | None:
    if isinstance(result, dict):
        if isinstance(result.get("id"), str):
            return result["id"]
        data = result.get("data")
        if isinstance(data, dict) and isinstance(data.get("id"), str):
            return data["id"]
        return None
    value = getattr(result, "id", None)
    return value if isinstance(value, str) else None


class _MirroringTools:
    """Proxies an agent's platform tools and reports every message it sends."""

    def __init__(self, inner: Any, on_sent) -> None:
        self._inner = inner
        self._on_sent = on_sent

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def execute_tool_call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        result = await self._inner.execute_tool_call(tool_name, arguments)
        if tool_name == BandTool.SEND_MESSAGE:
            message_id = _message_id(result)
            if message_id:
                try:
                    await self._on_sent(arguments, message_id)
                except Exception:
                    logger.exception("Failed to mirror an outgoing agent message")
        return result


class RoleAgentAdapter(AnthropicAdapter):
    def __init__(self, config: AnthropicAdapterConfig, *, profile_id: int, sink: AgentSink, **features):
        super().__init__(config, **features)
        self._profile_id = profile_id
        self._sink = sink

    async def on_message(
        self,
        msg,
        tools,
        history,
        participants_msg,
        contacts_msg,
        *,
        is_session_bootstrap: bool,
        room_id: str,
    ) -> None:
        async def on_sent(arguments: dict[str, Any], message_id: str) -> None:
            await self._sink.agent_message(
                self._profile_id,
                room_id,
                str(arguments.get("content", "")),
                [str(m) for m in arguments.get("mentions") or []],
                message_id,
            )

        await self._sink.agent_typing(self._profile_id, room_id, True)
        token = current_room.set(room_id)
        try:
            await super().on_message(
                msg,
                _MirroringTools(tools, on_sent),
                history,
                participants_msg,
                contacts_msg,
                is_session_bootstrap=is_session_bootstrap,
                room_id=room_id,
            )
        finally:
            current_room.reset(token)
            await self._sink.agent_typing(self._profile_id, room_id, False)


@dataclass
class _Running:
    agent: Agent
    task: asyncio.Task


class AgentManager:
    def __init__(self, sink: AgentSink) -> None:
        self._sink = sink
        self._running: dict[int, _Running] = {}
        self._band_ids: dict[int, str] = {}
        self.status: dict[int, dict[str, str | None]] = {}

    @property
    def local_band_ids(self) -> set[str]:
        return set(self._band_ids.values())

    async def _set_status(self, profile_id: int, state: str, error: str | None = None) -> None:
        self.status[profile_id] = {"state": state, "error": error}
        await self._sink.agent_status(profile_id, state, error)

    async def start(
        self,
        profile: AgentProfile,
        *,
        owner_name: str,
        owner_handle: str,
        tools: list[CustomToolDef] | None = None,
    ) -> None:
        assert profile.id is not None
        await self.stop(profile.id)
        config = AnthropicAdapterConfig(
            model=profile.model,
            provider_key=settings.anthropic_api_key or None,
            custom_section=build_prompt(
                name=profile.name,
                role=profile.role,
                instructions=profile.instructions,
                owner_name=owner_name,
                owner_handle=owner_handle,
            ),
        )
        adapter = RoleAgentAdapter(
            config,
            profile_id=profile.id,
            sink=self._sink,
            additional_tools=tools,
            emit={Emit.TOOL_CALLS},
            exclude_tools=[BandTool.CREATE_CHATROOM.value],
        )
        agent = Agent.create(
            adapter=adapter,
            agent_id=profile.band_agent_id,
            api_key=decrypt_secret(profile.band_api_key_enc),
            ws_url=settings.band_ws_url,
            rest_url=settings.band_rest_url,
        )
        self._band_ids[profile.id] = profile.band_agent_id
        task = asyncio.create_task(self._run(profile.id, agent), name=f"agent-{profile.id}")
        self._running[profile.id] = _Running(agent=agent, task=task)

    async def _run(self, profile_id: int, agent: Agent) -> None:
        await self._set_status(profile_id, "starting")
        try:
            await agent.start()
            await self._set_status(profile_id, "online")
            await agent.run_forever(install_signal_handlers=False)
            await self._set_status(profile_id, "stopped")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Agent %s crashed", profile_id)
            await self._set_status(profile_id, "error", describe_error(exc))
        finally:
            try:
                await agent.stop(timeout=5)
            except Exception:
                logger.exception("Agent %s did not stop cleanly", profile_id)

    async def stop(self, profile_id: int) -> None:
        running = self._running.pop(profile_id, None)
        self._band_ids.pop(profile_id, None)
        if running is None:
            return
        try:
            await running.agent.stop(timeout=5)
        except Exception:
            logger.exception("Agent %s did not stop cleanly", profile_id)
        running.task.cancel()
        await asyncio.gather(running.task, return_exceptions=True)
        await self._set_status(profile_id, "stopped")

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop(pid) for pid in list(self._running)))


@dataclass
class BandIdentity:
    id: str
    name: str
    handle: str


async def fetch_identity(api_key: str) -> BandIdentity:
    client = AsyncRestClient(api_key=api_key, base_url=settings.band_rest_url)
    try:
        me = (await client.agent_api_identity.get_agent_me(request_options=DEFAULT_REQUEST_OPTIONS)).data
        return BandIdentity(id=me.id, name=me.name, handle=me.handle or "")
    finally:
        await aclose_rest_client(client)

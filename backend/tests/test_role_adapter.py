"""Offline check that RoleAgentAdapter mirrors band_send_message calls and typing state."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from anthropic.types import Message, TextBlock, ToolUseBlock, Usage
from band.adapters.anthropic import AnthropicAdapterConfig
from band.core.types import PlatformMessage
from band.runtime.tools import ToolCallOutcome
from band.testing.fake_tools import FakeAgentTools

from app.agent_runner import RoleAgentAdapter


class SendingFakeTools(FakeAgentTools):
    """Returns the serialized MessageSentResponse the real tools return for a send."""

    async def execute_tool_call_structured(self, tool_name, arguments):
        if tool_name == "band_send_message":
            self.tool_calls.append({"tool_name": tool_name, "arguments": arguments})
            return ToolCallOutcome(
                value={"id": "band-msg-1", "success": True, "recipients": []}, ok=True
            )
        return await super().execute_tool_call_structured(tool_name, arguments)


class RecordingSink:
    def __init__(self) -> None:
        self.messages: list[tuple] = []
        self.typing: list[tuple] = []

    async def agent_message(self, profile_id, room_id, content, mentions, band_message_id):
        self.messages.append((profile_id, room_id, content, mentions, band_message_id))

    async def agent_typing(self, profile_id, room_id, active):
        self.typing.append((profile_id, room_id, active))

    async def agent_status(self, profile_id, state, error):
        pass


def _response(content, stop_reason):
    return Message(
        id="msg",
        type="message",
        role="assistant",
        model="test",
        content=content,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def test_outgoing_messages_are_mirrored():
    sink = RecordingSink()
    adapter = RoleAgentAdapter(
        AnthropicAdapterConfig(model="test", provider_key="test"),
        profile_id=7,
        sink=sink,
        emit=(),
    )
    replies = iter(
        [
            _response(
                [
                    ToolUseBlock(
                        type="tool_use",
                        id="t1",
                        name="band_send_message",
                        input={"content": "Hello Alice", "mentions": ["hub-1"]},
                    )
                ],
                "tool_use",
            ),
            _response([TextBlock(type="text", text="done")], "end_turn"),
        ]
    )

    async def fake_create(**_kwargs):
        return next(replies)

    adapter.client = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    tools = SendingFakeTools(
        room_id="room-1",
        participants=[
            {"id": "hub-1", "name": "Hub", "handle": "owner/hub", "type": "Agent", "status": "active", "role": "owner"}
        ],
    )
    msg = PlatformMessage(
        id="m1",
        room_id="room-1",
        content="Alice: @Writer say hi",
        sender_id="hub-1",
        sender_type="Agent",
        sender_name="Hub",
        message_type="text",
        metadata=None,
        created_at=datetime.now(timezone.utc),
    )

    asyncio.run(
        adapter.on_message(
            msg, tools, [], None, None, is_session_bootstrap=True, room_id="room-1"
        )
    )

    assert sink.messages == [(7, "room-1", "Hello Alice", ["hub-1"], "band-msg-1")]
    assert sink.typing == [(7, "room-1", True), (7, "room-1", False)]

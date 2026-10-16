"""Offline contract tests for cloud lifecycle, desktop replay protection, and image tools."""
from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from daytona import DaytonaForbiddenError, DaytonaNotFoundError, DaytonaRateLimitError
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlmodel import select

import app.computer as computer_module
from app.agent_tools import tools_for
from app.auth import encrypt_secret, issue_token
from app.computer import ComputerUnavailable, TaskComputer, sandbox_record, save_sandbox
from app.harness import build_harness
from app.main import app
from app.models import AgentProfile, AgentTask, Channel, ChannelMember, User, db, init_db
from app.tasks import enqueue
from app.worker import execute
from test_tasks import ScriptedModel


@pytest.fixture
def desktop_job(monkeypatch):
    init_db()
    def forbid_live_daytona(*args, **kwargs):
        raise AssertionError("Offline tests must not initialize a live Daytona client")
    monkeypatch.setattr(computer_module.AsyncDaytona, "__init__", forbid_live_daytona)
    unique = uuid4().hex
    with db() as session:
        agent = AgentProfile(name=f"Desktop-{unique[:8]}", band_agent_id=unique, role="master", is_master=True,
                             band_api_key_enc=encrypt_secret("offline"))
        channel = Channel(name=unique)
        user = User(username=unique, display_name="Desktop Tester", password_hash="unused")
        session.add_all([agent, channel, user])
        session.commit()
        for row in (agent, channel, user):
            session.refresh(row)
        session.add(ChannelMember(channel_id=channel.id, member_type="human", member_id=user.id))
        session.commit()
    task = enqueue(agent_id=agent.id, channel_id=channel.id, room_id=unique, source_id=unique,
                   prompt="Use the computer to verify the page")
    monkeypatch.setattr(computer_module, "settings", replace(computer_module.settings, daytona_api_key="offline-test"))
    client = SimpleNamespace()
    screen = base64.b64encode(b"offline-test-image").decode()
    sandbox = SimpleNamespace(id=f"sandbox-{unique}", state="started", labels={},
        computer_use=SimpleNamespace(
            start=AsyncMock(),
            display=SimpleNamespace(get_info=AsyncMock(return_value=SimpleNamespace(displays=[
                SimpleNamespace(width=1280, height=800, is_active=True)]))),
            screenshot=SimpleNamespace(take_compressed=AsyncMock(return_value=SimpleNamespace(screenshot=screen))),
            mouse=SimpleNamespace(click=AsyncMock(), move=AsyncMock(), drag=AsyncMock(), scroll=AsyncMock()),
            keyboard=SimpleNamespace(type=AsyncMock(), press=AsyncMock())),
        fs=SimpleNamespace(
            upload_file=AsyncMock(), create_folder=AsyncMock(),
            list_files=AsyncMock(return_value=[
                SimpleNamespace(name="notes.txt", path="/home/daytona/notes.txt", is_dir=False, size=12,
                                modified_at="2026-10-16T10:00:00Z"),
                SimpleNamespace(name="Downloads", path="/home/daytona/Downloads", is_dir=True, size=0,
                                modified_at=None)]),
            get_file_info=AsyncMock(side_effect=lambda path, **kwargs: SimpleNamespace(
                is_dir=False, size=12, path=f"/home/daytona/{path}" if not path.startswith("/") else path)),
            # Like the real SDK, download_file accepts positional arguments only.
            download_file=AsyncMock(side_effect=lambda *args: b"hello notes")),
        get_work_dir=AsyncMock(return_value="/home/daytona"),
        create_signed_preview_url=AsyncMock(return_value=SimpleNamespace(
            url="https://6080-signedtoken.proxy.example", token="signedtoken")),
        process=SimpleNamespace(exec=AsyncMock(return_value=SimpleNamespace(exit_code=0,
                               result=json.dumps({"exit_code": 0, "stdout": "2\n", "stderr": ""})))),
        start=AsyncMock(), stop=AsyncMock())
    client.allocated = False
    async def get(name, **kwargs):
        if not client.allocated:
            raise DaytonaNotFoundError("not found")
        return sandbox
    async def create(params, **kwargs):
        sandbox.labels = params.labels
        client.allocated = True
        return sandbox
    client.get = AsyncMock(side_effect=get)
    client.create = AsyncMock(side_effect=create)
    client.close = AsyncMock()
    controller = TaskComputer(task, client_factory=lambda config: client)
    return task, agent, user, controller, client, sandbox


def frame_id(blocks):
    return json.loads(blocks[0]["text"])["screen_id"]


def test_computer_create_observe_act_and_stop(desktop_job):
    task, _, _, computer, client, sandbox = desktop_job
    async def scenario():
        async with computer:
            screenshot = await computer.screenshot()
            assert screenshot[1]["type"] == "image_url"
            original = frame_id(screenshot)
            after = await computer.action(screen_id=original, action="click", x=200, y=100)
            assert frame_id(after) != original
            sandbox.computer_use.mouse.click.assert_awaited_once_with(200, 100, "left", False, request_timeout=20)
            params = client.create.call_args.args[0]
            assert params.public is False
            assert params.env_vars == {}
            assert params.auto_stop_interval > 0 and params.auto_delete_interval > 0
            assert sandbox_record(computer.key).sandbox_id == sandbox.id
        assert sandbox_record(computer.key).status == "stopped"
        assert sandbox_record(computer.key).latest_image
        sandbox.stop.assert_awaited_once()
        client.close.assert_awaited_once()
    asyncio.run(scenario())


def test_screen_replay_and_ambiguous_click_do_not_repeat_input(desktop_job):
    task, _, _, computer, _, sandbox = desktop_job
    async def scenario():
        async with computer:
            token = frame_id(await computer.screenshot())
            sandbox.computer_use.mouse.click.side_effect = TimeoutError("response lost")
            with pytest.raises(TimeoutError):
                await computer.action(screen_id=token, action="click", x=20, y=20)
            assert sandbox_record(computer.key).frame_id == ""
            with pytest.raises(ComputerUnavailable, match="already attempted"):
                await computer.action(screen_id=token, action="click", x=20, y=20)
            assert sandbox.computer_use.mouse.click.await_count == 1
    asyncio.run(scenario())


def test_desktop_calls_are_serialized_and_stale_helper_rejected(desktop_job):
    _, _, _, computer, _, sandbox = desktop_job
    async def scenario():
        async with computer:
            token = frame_id(await computer.screenshot())
            results = await asyncio.gather(
                computer.action(screen_id=token, action="type", text="first"),
                computer.action(screen_id=token, action="type", text="second"), return_exceptions=True)
            assert sum(isinstance(result, ComputerUnavailable) for result in results) == 1
            assert sandbox.computer_use.keyboard.type.await_count == 1
    asyncio.run(scenario())


def test_restart_reattaches_stopped_computer_without_creation(desktop_job):
    task, _, _, computer, client, sandbox = desktop_job
    async def scenario():
        async with computer:
            await computer.screenshot()
        sandbox.state = "stopped"
        second = TaskComputer(task, client_factory=lambda config: client)
        async with second:
            await second.screenshot()
        assert client.create.await_count == 1
        sandbox.start.assert_awaited_once_with(timeout=60)
        assert client.get.call_args.args[0] == sandbox.id
    asyncio.run(scenario())


def test_uncertain_create_recovers_by_persisted_name(desktop_job):
    task, _, _, computer, client, sandbox = desktop_job
    save_sandbox(computer.key, name=computer.sandbox_name())  # Crashed after allocation, before saving the ID.
    sandbox.labels = computer.labels()
    client.allocated = True
    async def scenario():
        async with computer:
            await computer.screenshot()
        client.create.assert_not_awaited()
        assert client.get.call_args.args[0] == computer.sandbox_name()
    asyncio.run(scenario())


def test_expired_computer_is_replaced_with_a_notice_and_foreign_one_refused(desktop_job):
    task, _, _, computer, client, sandbox = desktop_job
    save_sandbox(computer.key, name=computer.sandbox_name(), sandbox_id="expired", status="stopped")
    run_python = next(t for t in tools_for(task, computer) if t.name == "run_python")
    async def scenario():
        async with computer:
            result = await run_python.ainvoke({"code": "print(2)"})
            assert "previous computer in this channel had expired" in result["notice"]
            assert client.create.await_count == 1
            assert sandbox_record(computer.key).sandbox_id == sandbox.id
            assert "notice" not in await run_python.ainvoke({"code": "print(3)"})  # Reported once.
        sandbox.labels = {**sandbox.labels, "workspace": "someone-else"}
        other = TaskComputer(task, client_factory=lambda config: client)
        async with other:
            with pytest.raises(ComputerUnavailable, match="ownership"):
                await other.screenshot()
        assert client.create.await_count == 1
    asyncio.run(scenario())


def test_computer_persists_per_agent_and_channel(desktop_job):
    task, agent, _, computer, client, sandbox = desktop_job
    later = enqueue(agent_id=agent.id, channel_id=task.channel_id, room_id=task.room_id,
                    source_id=f"later-{task.id}", prompt="Use the same computer")
    with db() as session:
        elsewhere_channel = Channel(name=uuid4().hex)
        session.add(elsewhere_channel)
        session.commit()
        session.refresh(elsewhere_channel)
    elsewhere = enqueue(agent_id=agent.id, channel_id=elsewhere_channel.id, room_id=task.room_id,
                        source_id=f"elsewhere-{task.id}", prompt="Different channel")
    async def scenario():
        async with computer:
            await computer.command("echo first")
        sandbox.state = "stopped"
        second = TaskComputer(later, client_factory=lambda config: client)
        assert second.key == computer.key
        async with second:
            await second.command("echo second")
        assert client.create.await_count == 1  # Reattached to the same computer, restarted after stop.
        sandbox.start.assert_awaited_once_with(timeout=60)
    asyncio.run(scenario())
    other = TaskComputer(elsewhere, client_factory=lambda config: client)
    assert other.key != computer.key and other.sandbox_name() != computer.sandbox_name()
    assert other.labels()["channel"] == str(elsewhere_channel.id)


def test_coordinates_checked_and_commands_run_only_remotely(desktop_job):
    _, _, _, computer, _, sandbox = desktop_job
    async def scenario():
        async with computer:
            token = frame_id(await computer.screenshot())
            with pytest.raises(ComputerUnavailable, match="outside"):
                await computer.action(screen_id=token, action="click", x=1300, y=20)
            sandbox.computer_use.mouse.click.assert_not_awaited()
            assert (await computer.command("print(1+1)", python=True))["stdout"] == "2\n"
            command = sandbox.process.exec.call_args.args[0]
            assert "python3 -c" in command and "subprocess.Popen" in command
            assert sandbox.process.exec.call_args.kwargs["timeout"] == 40
    asyncio.run(scenario())


def test_screenshot_tool_returns_image_to_real_deepagents_loop(tmp_path, desktop_job):
    task, profile, _, computer, _, sandbox = desktop_job
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "see-1", "name": "computer_screenshot", "args": {}}]),
        AIMessage(content="Observed the desktop."),
    ])
    async def scenario():
        async with computer:
            async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "images.db")) as saver:
                await saver.setup()
                result = await execute(task, saver, computer=computer, factory=lambda t, p, c, **kwargs:
                    build_harness(t, p, c, model=model, computer=computer))
                assert result == "Observed the desktop."
                graph = build_harness(task, profile, saver, model=model, computer=computer)
                snapshot = await graph.aget_state({"configurable": {"thread_id": task.id}})
                tool_message = next(m for m in snapshot.values["messages"] if getattr(m, "name", "") == "computer_screenshot")
                assert tool_message.content[1]["type"] == "image_url"
        sandbox.stop.assert_awaited_once()
    asyncio.run(scenario())


def test_computer_endpoint_requires_channel_membership(desktop_job):
    task, _, user, computer, _, sandbox = desktop_job
    asyncio.run(computer.screenshot())
    asyncio.run(computer.close())
    with db() as session:
        outsider = User(username=uuid4().hex, display_name="Outsider", password_hash="unused")
        session.add(outsider)
        session.commit()
        session.refresh(outsider)
    url = f"/api/tasks/{task.id}/computer"
    with TestClient(app) as client:
        assert client.get(url).status_code == 401
        assert client.get(url, headers={"Authorization": f"Bearer {issue_token(outsider.id)}"}).status_code == 403
        response = client.get(url, headers={"Authorization": f"Bearer {issue_token(user.id)}"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["sandbox_id"] == sandbox.id
        assert response.json()["image"].startswith("data:image/jpeg;base64,")
        assert "frame_id" not in response.json()


def test_channel_lists_agent_computers_without_screenshots(desktop_job):
    task, agent, user, computer, _, _ = desktop_job
    url = f"/api/channels/{task.channel_id}/computers"
    auth = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as client:
        assert client.get(url, headers=auth).json() == []
        asyncio.run(computer.screenshot())
        with db() as session:
            stored = session.get(AgentTask, task.id)
            stored.status = "running"
            session.add(stored)
            session.commit()
        [listed] = client.get(url, headers=auth).json()
    assert listed["agent_id"] == agent.id and listed["task_id"] == task.id
    assert listed["status"] == "running" and listed["agent_working"] is True
    assert listed["last_action"] and "image" not in listed
    asyncio.run(computer.close())


def test_browser_launch_and_file_export_survive_final_result(tmp_path, desktop_job):
    task, _, _, computer, _, sandbox = desktop_job
    async def scenario():
        async with computer:
            blocks = await computer.open_browser("https://example.com")
            assert blocks[1]["type"] == "image_url"
            browser_command = sandbox.process.exec.call_args.args[0]
            assert "https://example.com" in browser_command
            assert "nohup" in browser_command and "</dev/null" in browser_command
            assert "--disable-quic" in browser_command
            await computer.write_file("/tmp/report.txt", "Verified result")
            sandbox.fs.upload_file.assert_awaited_once_with(b"Verified result", "/tmp/report.txt", timeout=30)
            sandbox.process.exec.return_value.result = base64.b64encode(b"Verified result").decode()
            exported = await computer.export_file("/tmp/report.txt")
            assert exported["bytes"] == 15
        model = ScriptedModel(responses=[AIMessage(content="Your report is ready.")])
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "exports.db")) as saver:
            await saver.setup()
            result = await execute(task, saver, factory=lambda t, p, c, **kwargs:
                build_harness(t, p, c, model=model, **kwargs))
            assert "ready" in result
        with db() as session:
            saved = session.get(AgentTask, task.id)
            assert saved.artifacts == [{"path": "/tmp/report.txt", "content": "Verified result"}]
    asyncio.run(scenario())


def test_cancellation_stops_desktop_and_closes_sdk(desktop_job):
    task, _, _, computer, client, sandbox = desktop_job
    async def scenario():
        ready = asyncio.Event()
        async def task_body():
            async with computer:
                await computer.screenshot()
                ready.set()
                await asyncio.sleep(30)
        running = asyncio.create_task(task_body())
        await ready.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert sandbox_record(computer.key).status == "stopped"
        sandbox.stop.assert_awaited_once()
        client.close.assert_awaited_once()
    asyncio.run(scenario())


def test_failed_stop_is_visible_without_losing_task_result(desktop_job):
    task, _, _, computer, client, sandbox = desktop_job
    sandbox.stop.side_effect = TimeoutError("cloud stop unconfirmed")
    async def scenario():
        async with computer:
            await computer.screenshot()
        assert sandbox_record(computer.key).status == "stop_failed"
        assert "not confirmed" in sandbox_record(computer.key).error
        client.close.assert_awaited_once()
    asyncio.run(scenario())


def test_computer_subagent_can_observe_desktop(tmp_path, desktop_job):
    from pydantic import Field
    task, _, _, computer, _, _ = desktop_job
    class RecordingModel(ScriptedModel):
        observed: list = Field(default_factory=list)
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.observed.append(list(messages))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
    model = RecordingModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "delegate-desktop", "name": "task", "args": {
            "subagent_type": "computer", "description": "Inspect the desktop and report what is visible."}}]),
        AIMessage(content="", tool_calls=[{"id": "observe-desktop", "name": "computer_screenshot", "args": {}}]),
        AIMessage(content="Desktop inspected."),
        AIMessage(content="The computer specialist verified the desktop."),
    ])
    async def scenario():
        async with computer:
            async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "computer-helper.db")) as saver:
                await saver.setup()
                answer = await execute(task, saver, computer=computer, factory=lambda t, p, c, **kwargs:
                    build_harness(t, p, c, model=model, computer=computer))
        assert "specialist verified" in answer
        assert any(getattr(message, "name", None) == "computer_screenshot"
                   and isinstance(message.content, list) and message.content[1]["type"] == "image_url"
                   for turn in model.observed for message in turn)
    asyncio.run(scenario())


def test_cleanup_recovers_computer_when_graph_already_finished(desktop_job):
    task, _, _, computer, client, sandbox = desktop_job
    save_sandbox(computer.key, sandbox_id=sandbox.id, status="running")
    client.allocated = True
    sandbox.labels = computer.labels()
    asyncio.run(computer.close())
    sandbox.stop.assert_awaited_once_with(timeout=20)
    sandbox.start.assert_not_awaited()
    assert sandbox_record(computer.key).status == "stopped"


def test_quota_failures_explain_storage_limits(desktop_job):
    _, _, _, computer, client, _ = desktop_job
    client.create.side_effect = DaytonaForbiddenError("Total disk quota exceeded", 403)
    async def scenario():
        async with computer:
            with pytest.raises(ComputerUnavailable, match="storage or quota limit reached"):
                await computer.screenshot()
        client.create.side_effect = DaytonaRateLimitError("Rate limit exceeded", 429)
        retry = TaskComputer(computer.task, client_factory=lambda config: client)
        async with retry:
            with pytest.raises(DaytonaRateLimitError):  # Rate limits are not storage problems.
                await retry.screenshot()
    asyncio.run(scenario())



def test_live_view_signs_novnc_link_without_creating_a_computer(desktop_job):
    _, _, _, computer, client, sandbox = desktop_job
    async def scenario():
        with pytest.raises(ComputerUnavailable, match="no computer in this channel yet"):
            await computer.live_view()
        client.create.assert_not_awaited()
        async with computer:
            await computer.command("echo setup")  # The agent created its computer earlier.
        view = await computer.live_view()
        await computer.detach()
        assert view["url"] == "https://6080-signedtoken.proxy.example/vnc.html"
        sandbox.create_signed_preview_url.assert_awaited_once_with(6080, expires_in_seconds=900)
        sandbox.computer_use.start.assert_awaited()
    asyncio.run(scenario())


def test_human_control_pauses_agent_input_but_not_observation(desktop_job):
    from app.computer import active_control, release_control, take_control
    _, _, user, computer, _, sandbox = desktop_job
    async def scenario():
        async with computer:
            token = frame_id(await computer.screenshot())
            take_control(computer.key, user_id=user.id, user_name="Desktop Tester")
            assert sandbox_record(computer.key).frame_id == ""  # The person changes the screen.
            with pytest.raises(ComputerUnavailable, match="Desktop Tester is using this computer"):
                await computer.action(screen_id=token, action="click", x=5, y=5)
            with pytest.raises(ComputerUnavailable, match="is using this computer"):
                await computer.command("rm -rf /tmp/x")
            with pytest.raises(ComputerUnavailable, match="is using this computer"):
                await computer.open_browser("https://example.com")
            await computer.screenshot()  # Watching is still allowed.
            sandbox.computer_use.mouse.click.assert_not_awaited()
            release_control(computer.key, user_name="Desktop Tester")
            assert active_control(computer.key) is None
            fresh = frame_id(await computer.screenshot())
            await computer.action(screen_id=fresh, action="click", x=5, y=5)
            sandbox.computer_use.mouse.click.assert_awaited_once()
    asyncio.run(scenario())


def test_people_can_browse_download_and_upload_files(desktop_job):
    _, _, _, computer, _, sandbox = desktop_job
    async def scenario():
        async with computer:
            await computer.command("echo setup")
        entries = await computer.list_files("")
        assert [e["name"] for e in entries] == ["Downloads", "notes.txt"]  # Folders first.
        assert await computer.download("/home/daytona/notes.txt") == b"hello notes"
        path = await computer.upload("..\\..\\secret plan?.pdf", b"%PDF")
        assert path == "/home/daytona/uploads/secret plan_.pdf"
        sandbox.fs.upload_file.assert_awaited_with(b"%PDF", "uploads/secret plan_.pdf", timeout=120)
        sandbox.fs.get_file_info.side_effect = lambda path, **kwargs: SimpleNamespace(is_dir=False, size=26 * 1024 * 1024)
        with pytest.raises(ComputerUnavailable, match="25 MB"):
            await computer.download("/home/daytona/huge.bin")
        with pytest.raises(ComputerUnavailable, match="25 MB"):
            await computer.upload("big.bin", b"x" * (25 * 1024 * 1024 + 1))
        await computer.detach()
    asyncio.run(scenario())


def test_live_computer_api_requires_membership_and_coordinates_control(desktop_job, monkeypatch):
    import app.main as main_module
    from app.models import Message
    task, _, user, computer, client, sandbox = desktop_job
    asyncio.run(_create_computer(computer))
    monkeypatch.setattr(main_module, "computer_factory", lambda t: TaskComputer(t, client_factory=lambda config: client))
    with db() as session:
        outsider = User(username=uuid4().hex, display_name="Outsider", password_hash="unused")
        session.add(outsider)
        session.commit()
        session.refresh(outsider)
    member = {"Authorization": f"Bearer {issue_token(user.id)}"}
    base = f"/api/tasks/{task.id}/computer"
    with TestClient(app) as http:
        assert http.post(base + "/live", json={}, headers={"Authorization": f"Bearer {issue_token(outsider.id)}"}).status_code == 403
        view = http.post(base + "/live", json={"control": True}, headers=member)
        assert view.status_code == 200 and view.headers["cache-control"] == "no-store"
        assert view.json()["url"].endswith("/vnc.html") and view.json()["control"]["mine"] is True
        assert http.get(base, headers=member).json()["control"]["user_name"] == "Desktop Tester"
        assert http.delete(base + "/control", headers=member).status_code == 200
        assert http.get(base, headers=member).json()["control"] is None
        assert http.get(base + "/files", headers=member).json()[0]["name"] == "Downloads"
        download = http.get(base + "/files/download", params={"path": "/home/daytona/notes.txt"}, headers=member)
        assert download.content == b"hello notes" and 'filename="notes.txt"' in download.headers["content-disposition"]
        uploaded = http.post(base + "/files", params={"name": "brief.txt"}, content=b"Read me", headers=member)
        assert uploaded.json()["path"] == "/home/daytona/uploads/brief.txt"
        with db() as session:
            notes = [m.content for m in session.exec(select(Message).where(Message.channel_id == task.channel_id))]
        assert any("took control" in n for n in notes) and any("handed" in n and "back" in n for n in notes)
        assert any("uploaded brief.txt" in n and "/home/daytona/uploads/brief.txt" in n for n in notes)
        with db() as session:
            stored = session.get(AgentTask, task.id)
            stored.status = "running"
            session.add(stored)
            session.commit()
        assert http.post(base + "/stop", headers=member).status_code == 409
        with db() as session:
            stored = session.get(AgentTask, task.id)
            stored.status = "completed"
            session.add(stored)
            session.commit()
        sandbox.stop.reset_mock()
        assert http.post(base + "/stop", headers=member).status_code == 200
        sandbox.stop.assert_awaited_once()


async def _create_computer(computer):
    async with computer:
        await computer.command("echo setup")

from __future__ import annotations
import asyncio
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlmodel import select
from app.agent_tools import tools_for
from app.auth import encrypt_secret, issue_token
from app.harness import build_harness
from app.main import app
from app.models import AgentProfile, AgentTask, Channel, ChannelMember, Message, User, db, init_db
from app.tasks import enqueue, recover_tasks, update_task
from app.worker import complete, execute, process


class ScriptedModel(BaseChatModel):
    """Run the actual Deep Agents graph and tools without provider credentials."""
    responses: list
    index: int = 0

    @property
    def _llm_type(self):
        return "offline-scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        response = self.responses[self.index]
        self.index += 1
        if isinstance(response, Exception):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])


@pytest.fixture
def job():
    init_db()
    key = uuid4().hex
    with db() as session:
        agent = AgentProfile(name=f"Agent-{key[:8]}", role="master", is_master=True,
                             band_agent_id=key, band_api_key_enc=encrypt_secret("offline"))
        channel = Channel(name=key, band_room_id=key)
        user = User(username=key, display_name="Tester", password_hash="unused")
        session.add_all([agent, channel, user])
        session.commit()
        session.refresh(agent)
        session.refresh(channel)
        session.refresh(user)
        session.add(ChannelMember(channel_id=channel.id, member_type="human", member_id=user.id))
        session.commit()
    task = enqueue(agent_id=agent.id, channel_id=channel.id, room_id=key,
                   source_id=key, prompt="Create a draft and verify it")
    return task, agent, user


def test_checkpoint_survives_failure_and_new_worker(tmp_path, job):
    task, _, _ = job
    first = ScriptedModel(responses=[AIMessage(content="", tool_calls=[{
        "name": "write_file", "args": {"file_path": "/draft.txt", "content": "Verified draft"}, "id": "file-1"
    }]), RuntimeError("simulated worker failure")])
    async def scenario():
        path = str(tmp_path / "checkpoints.db")
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            await saver.setup()
            update_task(task.id, status="running")
            with pytest.raises(RuntimeError, match="simulated"):
                await execute(task, saver, factory=lambda t, p, c, **kw: build_harness(t, p, c, model=first, **kw))
        recover_tasks()
        with db() as session:
            assert session.get(AgentTask, task.id).status == "queued"
        second = ScriptedModel(responses=[AIMessage(content="Finished draft")])
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            result = await execute(task, saver, factory=lambda t, p, c, **kw: build_harness(t, p, c, model=second, **kw))
            assert result == "Finished draft"
        with db() as session:
            assert session.get(AgentTask, task.id).artifacts == [{"path": "/draft.txt", "content": "Verified draft"}]
    asyncio.run(scenario())


def test_real_subagent_delegation(tmp_path, job):
    task, _, _ = job
    model = ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "task", "args": {
            "subagent_type": "critic", "description": "Review: 2 + 2 = 4. Return a verdict."}, "id": "delegate-1"}]),
        AIMessage(content="The arithmetic is correct."),
        AIMessage(content="Reviewed: 2 + 2 = 4."),
    ])
    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "subagents.db")) as saver:
            await saver.setup()
            result = await execute(task, saver, factory=lambda t, p, c, **kw: build_harness(t, p, c, model=model, **kw))
            assert "Reviewed" in result
            graph = build_harness(task, job[1], saver, model=model)
            snapshot = await graph.aget_state({"configurable": {"thread_id": task.id}})
            messages = snapshot.values["messages"]
            assert any(getattr(m, "name", None) == "task" for m in messages)
    asyncio.run(scenario())


def test_memory_is_persistent_and_scoped(job):
    task, _, _ = job
    tools = {t.name: t for t in tools_for(task)}
    tools["remember"].invoke({"key": "style", "content": "Concise prose; source: channel request"})
    assert tools["recall_memory"].invoke({"query": "style"})[0]["source_task_id"] == task.id
    other = task.model_copy(update={"channel_id": task.channel_id + 10000})
    assert tools_for(other)[0].invoke({}) == []
    other = task.model_copy(update={"agent_id": task.agent_id + 10000})
    assert tools_for(other)[0].invoke({}) == []


def test_completion_is_idempotent_and_cancellation_wins(job):
    task, _, _ = job
    update_task(task.id, status="running")
    complete(task, "Answer")
    complete(task, "Answer")
    with db() as session:
        assert len(session.exec(select(Message).where(Message.band_message_id == f"task:{task.id}")).all()) == 1
    second = enqueue(agent_id=task.agent_id, channel_id=task.channel_id, room_id=task.room_id,
                     source_id="cancel", prompt="cancel me")
    update_task(second.id, status="running", cancel_requested=True)
    complete(second, "Must not appear")
    with db() as session:
        assert session.get(AgentTask, second.id).status == "cancelled"
        assert not session.exec(select(Message).where(Message.band_message_id == f"task:{second.id}")).first()


def test_task_api_auth_control_and_memory_deletion(job):
    task, _, user = job
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as client:
        assert client.get(f"/api/tasks/{task.id}").status_code == 401
        response = client.get(f"/api/channels/{task.channel_id}/tasks", headers=headers)
        assert response.status_code == 200
        assert "prompt" not in response.json()[0]
        assert client.post(f"/api/tasks/{task.id}/cancel", headers=headers).json()["status"] == "cancelled"
        assert client.post(f"/api/tasks/{task.id}/retry", headers=headers).json()["status"] == "queued"
        assert client.post(f"/api/tasks/{task.id}/retry", headers=headers).status_code == 409
        tools_for(task)[1].invoke({"key": "delete-me", "content": "temporary"})
        url = f"/api/channels/{task.channel_id}/agents/{task.agent_id}/memories"
        assert len(client.get(url, headers=headers).json()) == 1
        assert client.delete(url + "/delete-me", headers=headers).status_code == 200
        assert client.get(url, headers=headers).json() == []


def test_worker_cancels_active_execution(job):
    task, _, _ = job
    update_task(task.id, status="running", cancel_requested=True)
    async def slow(task, saver):
        await asyncio.sleep(30)
    asyncio.run(process(task, None, executor=slow))
    with db() as session:
        assert session.get(AgentTask, task.id).status == "cancelled"


def test_worker_shutdown_leaves_job_recoverable(job):
    task, _, _ = job
    update_task(task.id, status="running")
    async def slow(task, saver):
        await asyncio.sleep(30)
    async def scenario():
        running = asyncio.create_task(process(task, None, executor=slow))
        await asyncio.sleep(0.01)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    asyncio.run(scenario())
    with db() as session:
        assert session.get(AgentTask, task.id).status == "running"
    recover_tasks()
    with db() as session:
        assert session.get(AgentTask, task.id).status == "queued"


def test_unknown_band_delivery_is_not_retried(job, monkeypatch):
    from types import SimpleNamespace
    import app.worker as worker
    task, _, _ = job
    update_task(task.id, status="running", reply_to="owner")
    complete(task, "Saved answer")
    with db() as session:
        task = session.get(AgentTask, task.id)
    sends = []
    async def failed_send(*args, **kwargs):
        sends.append(kwargs)
        raise TimeoutError("lost response")
    async def close(client):
        pass
    monkeypatch.setattr(worker, "AsyncRestClient", lambda **kwargs: SimpleNamespace(
        agent_api_messages=SimpleNamespace(create_agent_chat_message=failed_send)))
    monkeypatch.setattr(worker, "aclose_rest_client", close)
    async def scenario():
        await worker.deliver(task)
        await worker.deliver(task)  # Even a stale pending task cannot repeat the send.
    asyncio.run(scenario())
    assert len(sends) == 1
    assert sends[0]["request_options"]["max_retries"] == 0
    with db() as session:
        saved = session.get(AgentTask, task.id)
        assert saved.delivery_status == "unknown"
        assert saved.result == "Saved answer"


def test_task_controls_reject_nonmembers(job):
    task, _, _ = job
    with db() as session:
        user = User(username=uuid4().hex, display_name="Nonmember", password_hash="unused")
        session.add(user)
        session.commit()
        session.refresh(user)
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as client:
        assert client.post(f"/api/tasks/{task.id}/cancel", headers=headers).status_code == 403
        assert client.post(f"/api/tasks/{task.id}/retry", headers=headers).status_code == 403


def test_worker_timeout_preserves_failure_for_manual_resume(job, monkeypatch):
    from types import SimpleNamespace
    import app.worker as worker
    task, _, _ = job
    update_task(task.id, status="running")
    monkeypatch.setattr(worker, "settings", SimpleNamespace(task_timeout=0.01))
    async def slow(task, saver):
        await asyncio.sleep(30)
    asyncio.run(worker.process(task, None, executor=slow))
    with db() as session:
        saved = session.get(AgentTask, task.id)
        assert saved.status == "failed"
        assert saved.error == "Time limit reached"


def test_missing_optional_tools_report_unavailable(job, monkeypatch):
    from types import SimpleNamespace
    import app.agent_tools as module
    task, _, _ = job
    monkeypatch.setattr(module, "settings", SimpleNamespace(exa_api_key=""))
    tools = {tool.name: tool for tool in tools_for(task)}
    async def scenario():
        assert "not configured" in (await tools["web_search"].ainvoke({"query": "test"}))["error"]
        assert "not configured" in (await tools["run_python"].ainvoke({"code": "print(2)"}))["error"]
    asyncio.run(scenario())


def test_worker_replies_reach_websocket_without_band(job):
    task, _, user = job
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={issue_token(user.id)}") as ws:
            update_task(task.id, status="running")
            complete(task, "Durable reply")
            event = ws.receive_json()
            while event["type"] != "message":
                event = ws.receive_json()
            assert event["message"]["content"] == "Durable reply"
            assert event["message"]["channel_id"] == task.channel_id


def test_local_mention_is_durable_before_band_and_echo_is_deduplicated(job):
    from types import SimpleNamespace
    from app.agent_runner import QueuedAgentAdapter
    from app.service import AppService
    task, agent, user = job
    svc = AppService()
    async def scenario():
        posted = await svc.post_human_message(user, task.channel_id, f"@{agent.name} verify the result")
        key = f"{agent.id}:{task.room_id}:app-message-{posted['id']}"
        with db() as session:
            saved = session.exec(select(AgentTask).where(AgentTask.source_key == key)).one()
            assert saved.status == "queued"
            before = len(session.exec(select(AgentTask).where(AgentTask.channel_id == task.channel_id)).all())
        adapter = QueuedAgentAdapter(profile_id=agent.id, reply_to="owner")
        await adapter.on_message(SimpleNamespace(id="band-echo", sender_id="owner",
            content=f"**Tester:** @{agent.name} verify the result"), None, [], None, None,
            is_session_bootstrap=True, room_id=task.room_id)
        with db() as session:
            assert len(session.exec(select(AgentTask).where(AgentTask.channel_id == task.channel_id)).all()) == before
    asyncio.run(scenario())

from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlmodel import select

from app.auth import issue_token
from app.main import app
from app.persistence.models import (AgentProfile, AgentTask, AuditEvent, Budget, ChannelMember, Message, TaskApproval,
                                    TaskUsage, User, WorkspaceRole, db, init_db)
from app.runtime.harness import build_harness
from app.tasks import next_task, update_task
from app.tasks.worker import execute, process, record_activity
from app.workspace.audit import summarize, summarize_args
from app.workspace.permissions import ensure_owner, role_of
from test_tasks import ScriptedModel, isolate_queue, job, teammate  # noqa: F401  (job is a fixture)


def _person(name: str, role: str | None = None, channel_id: int | None = None) -> tuple[User, dict]:
    """A new account, never the owner (the job fixture's user can be the suite's oldest account)."""
    init_db()
    with db() as session:
        user = User(username=uuid4().hex, display_name=name, password_hash="unused")
        session.add(user)
        session.commit()
        session.refresh(user)
        if role:
            session.add(WorkspaceRole(user_id=user.id, role=role))
        if channel_id:
            session.add(ChannelMember(channel_id=channel_id, member_type="human", member_id=user.id))
        session.commit()
    return user, _auth(user.id)


def _auth(user_id: int) -> dict:
    return {"Authorization": f"Bearer {issue_token(user_id)}"}


def _pending_approval(task: AgentTask) -> str:
    approval_id = uuid4().hex
    update_task(task.id, status="waiting")
    with db() as session:
        session.add(TaskApproval(id=approval_id, task_id=task.id, channel_id=task.channel_id, agent_id=task.agent_id,
                                 action="Email the client"))
        session.commit()
    return approval_id


def _activity(client: TestClient, channel_id: int, headers: dict, **params) -> list[dict]:
    """The channel's audit events, oldest first."""
    response = client.get(f"/api/channels/{channel_id}/audit", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return list(reversed(response.json()))


def test_oldest_account_owns_the_workspace_and_only_the_owner_changes_roles():
    _person("Seed")
    with db() as session:
        ensure_owner(session)
        ensure_owner(session)
        owners = session.exec(select(WorkspaceRole).where(WorkspaceRole.role == "owner")).all()
        oldest = session.exec(select(User).order_by(User.created_at, User.id)).first()
    assert [o.user_id for o in owners] == [oldest.id]
    _, admin = _person("Ada", "admin")
    max_user, member = _person("Max")
    owner = _auth(oldest.id)
    with TestClient(app) as client:
        assert client.put(f"/api/users/{max_user.id}/role", json={"role": "admin"}, headers=admin).status_code == 403
        assert client.put(f"/api/users/{oldest.id}/role", json={"role": "member"}, headers=owner).status_code == 409
        assert client.put(f"/api/users/{max_user.id}/role", json={"role": "owner"}, headers=owner).status_code == 422
        assert client.put(f"/api/users/{max_user.id}/role", json={"role": "admin"}, headers=owner).status_code == 200
        roles = client.get("/api/bootstrap", headers=member).json()["workspace_roles"]
        assert roles[str(max_user.id)] == "admin" and roles[str(oldest.id)] == "owner"
        assert client.put(f"/api/users/{max_user.id}/role", json={"role": "member"}, headers=owner).status_code == 200
        with db() as session:
            assert role_of(session, max_user.id) == "member"
        assert client.get("/api/audit", headers=member).status_code == 403
        changes = [e["detail"] for e in client.get("/api/audit", headers=owner).json() if e["action"] == "role_changed"]
        assert {"user": "Max", "role": "member"} in changes and {"user": "Max", "role": "admin"} in changes


def test_approvals_follow_the_channel_policy(job):
    task, _, _ = job
    _, member = _person("Max", channel_id=task.channel_id)
    _, admin = _person("Ada", "admin", task.channel_id)
    with TestClient(app) as client:
        first = _pending_approval(task)
        # No policy keeps the old behavior: any member decides.
        assert client.post(f"/api/approvals/{first}", json={"approve": True}, headers=member).status_code == 200
        url = f"/api/channels/{task.channel_id}/policy"
        assert client.put(url, json={"approvers": "admins"}, headers=member).status_code == 403
        assert client.put(url, json={"approvers": "admins"}, headers=admin).json() == {
            "approvers": "admins", "computer_control": "members"}
        channel = next(c for c in client.get("/api/bootstrap", headers=member).json()["channels"]
                       if c["id"] == task.channel_id)
        assert channel["policy"]["approvers"] == "admins"
        second = _pending_approval(task)
        denied = client.post(f"/api/approvals/{second}", json={"approve": True}, headers=member)
        assert denied.status_code == 403 and "admins" in denied.json()["detail"]
        assert client.post(f"/api/approvals/{second}", json={"approve": False}, headers=admin).status_code == 200
        actions = [e["action"] for e in _activity(client, task.channel_id, member)]
        assert actions == ["approval_approved", "policy_changed", "approval_denied"]
    with db() as session:
        notes = [m.content for m in session.exec(select(Message).where(Message.channel_id == task.channel_id))]
    assert any("only admins can approve agent actions" in n for n in notes)


def test_deleting_agents_and_taking_over_computers_need_permission(job):
    task, _, _ = job
    _, member = _person("Max", channel_id=task.channel_id)
    _, admin = _person("Ada", "admin", task.channel_id)
    writer = teammate()
    with TestClient(app) as client:
        assert client.delete(f"/api/agents/{writer.id}", headers=member).status_code == 403
        assert client.delete(f"/api/agents/{writer.id}", headers=admin).status_code == 200
        with db() as session:
            assert session.get(AgentProfile, writer.id) is None
        client.put(f"/api/channels/{task.channel_id}/policy", json={"computer_control": "admins"}, headers=admin)
        live = f"/api/tasks/{task.id}/computer/live"
        assert client.post(live, json={"control": True}, headers=member).status_code == 403
        # Watching stays open to members; it fails later only because no computer is configured.
        assert client.post(live, json={"control": False}, headers=member).status_code != 403
        events = client.get("/api/audit", headers=admin).json()
        assert any(e["action"] == "agent_deleted" and e["detail"] == {"agent": writer.name} for e in events)


def test_activity_lists_tool_calls_approvals_and_handoffs_in_order(tmp_path, job):
    task, _, user = job
    writer = teammate()
    model = ScriptedModel(responses=[
        AIMessage(content="", id="call-model-1", tool_calls=[{"name": "request_approval", "args": {
            "action": "Email the client", "details": "Use token=abc123secretvalue"}, "id": "ask-1"}],
            usage_metadata={"input_tokens": 1200, "output_tokens": 80, "total_tokens": 1280}),
        AIMessage(content="", id="call-model-2", tool_calls=[{"name": "ask_teammate", "args": {
            "teammate": writer.name, "request": "Draft the follow-up", "report_back": False}, "id": "hand-1"}],
            usage_metadata={"input_tokens": 1500, "output_tokens": 40, "total_tokens": 1540}),
        AIMessage(content="Emailed and handed off.", id="call-model-3",
                  usage_metadata={"input_tokens": 1600, "output_tokens": 10, "total_tokens": 1610}),
    ])
    path = str(tmp_path / "audit.db")

    def run():
        async def scenario():
            async with AsyncSqliteSaver.from_conn_string(path) as saver:
                await saver.setup()
                with db() as session:
                    current = session.get(AgentTask, task.id)
                update_task(task.id, status="running")
                await process(current, saver, executor=lambda t, s: execute(
                    t, s, factory=lambda t, p, c, **kw: build_harness(t, p, c, model=model, **kw)))
        asyncio.run(scenario())

    run()
    with db() as session:
        approval = session.exec(select(TaskApproval).where(TaskApproval.task_id == task.id)).one()
    member = _auth(user.id)
    with TestClient(app) as client:
        assert client.post(f"/api/approvals/{approval.id}", json={"approve": True}, headers=member).status_code == 200
        run()
        with db() as session:
            assert session.get(AgentTask, task.id).status == "completed"
        events = _activity(client, task.channel_id, member, task_id=task.id)
        assert [(e["action"], e["detail"].get("tool")) for e in events] == [
            ("tool_call", "request_approval"), ("approval_requested", None), ("approval_approved", None),
            ("tool_call", "ask_teammate"), ("handoff", None)]
        assert events[0]["detail"]["args"]["details"] == "Use token=[hidden]"
        assert events[1]["detail"]["details"] == "Use token=[hidden]"
        assert events[2]["actor_type"] == "human" and events[2]["actor_name"] == "Tester"
        assert events[4]["detail"]["teammate"] == writer.name
        listed = next(t for t in client.get(f"/api/channels/{task.channel_id}/tasks", headers=member).json()
                      if t["id"] == task.id)
        assert listed["usage"] == {"input_tokens": 4300, "output_tokens": 130, "cost": None}
        usage = client.get(f"/api/channels/{task.channel_id}/usage", headers=member).json()
        assert usage["tokens"] == 4430 and usage["budget"] is None


def test_tool_calls_and_usage_are_recorded_once_and_without_secrets(job):
    task, agent, _ = job
    message = AIMessage(content=[
        {"type": "text", "text": "Checking"},
        {"type": "server_tool_use", "id": "srv-1", "name": "code_execution", "input": {"code": "print(1)"}},
    ], id="model-call-9", tool_calls=[{"name": "fetch_page", "id": "fetch-1", "args": {
        "url": "https://example.com/?api_key=abcdef123456", "api_key": "sk-live", "Authorization": "Bearer abc",
        "key": "style"}}], usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    record_activity(task, (), message, agent.model)
    record_activity(task, (), message, agent.model)  # A replayed stream update.
    record_activity(task, ("tools:helper",), AIMessage(content="", id="helper-1", tool_calls=[
        {"name": "web_search", "id": "search-1", "args": {"query": "news"}}]), agent.model)
    with db() as session:
        events = session.exec(select(AuditEvent).where(AuditEvent.task_id == task.id).order_by(AuditEvent.id)).all()
        usage = session.exec(select(TaskUsage).where(TaskUsage.task_id == task.id)).all()
    assert [e.detail["tool"] for e in events] == ["fetch_page", "code_execution", "web_search"]
    assert events[0].detail["args"] == {"url": "https://example.com/?api_key=[hidden]", "api_key": "[hidden]",
                                        "Authorization": "[hidden]", "key": "style"}
    assert events[2].detail["via"] == "helper"
    assert [(u.input_tokens, u.output_tokens, u.model) for u in usage] == [(10, 5, agent.model)]
    assert summarize("x" * 500).endswith("…") and len(summarize("x" * 500)) == 200
    assert summarize_args({"cmd": "curl -H 'Authorization: Bearer sk-abcdefghijklmnop'"})["cmd"].count("[hidden]") == 1


def test_channel_budget_pauses_new_tasks_until_an_admin_raises_it(job):
    task, agent, _ = job
    _, member = _person("Max", channel_id=task.channel_id)
    _, admin = _person("Ada", "admin")
    isolate_queue({task.id})
    with db() as session:
        session.add(TaskUsage(task_id=uuid4().hex, channel_id=task.channel_id, agent_id=agent.id,
                              message_id="earlier", model=agent.model, input_tokens=4000, output_tokens=1000))
        session.commit()
    budget = {"scope": "channel", "scope_id": task.channel_id}
    with TestClient(app) as client:
        assert client.put("/api/budgets", json={**budget, "daily_tokens": 5000}, headers=member).status_code == 403
        assert client.put("/api/budgets", json={**budget, "daily_tokens": 5000}, headers=admin).status_code == 200
        assert next_task() is None
        with db() as session:
            progress = session.get(AgentTask, task.id).progress
        assert progress.startswith("Paused: #") and "5,000 tokens" in progress
        usage = client.get(f"/api/channels/{task.channel_id}/usage", headers=member).json()
        assert usage["tokens"] == 5000 and usage["budget"] == 5000
        assert usage["agents"][0]["agent_id"] == agent.id and usage["agents"][0]["tokens"] == 5000
        assert client.put("/api/budgets", json={**budget, "daily_tokens": 10000}, headers=admin).status_code == 200
        assert next_task().id == task.id
        changes = [e["detail"]["daily_tokens"] for e in _activity(client, task.channel_id, member)
                   if e["action"] == "budget_changed"]
        assert changes == [5000, 10000]
        assert client.put("/api/budgets", json={**budget, "daily_tokens": None}, headers=admin).status_code == 200
        with db() as session:
            assert session.get(Budget, ("channel", task.channel_id)) is None


def test_running_task_stops_when_its_agent_budget_runs_out(job):
    task, agent, _ = job
    update_task(task.id, status="running")

    async def scenario():
        async def spend(task, saver):
            await asyncio.sleep(0.6)
            with db() as session:
                session.add(TaskUsage(task_id=task.id, channel_id=task.channel_id, agent_id=agent.id,
                                      message_id="big", model=agent.model, input_tokens=3000, output_tokens=0))
                session.commit()
            await asyncio.sleep(30)
        with db() as session:
            session.add(Budget(scope="agent", scope_id=agent.id, daily_tokens=2000))
            session.commit()
        await asyncio.wait_for(process(task, None, executor=spend), timeout=10)
    asyncio.run(scenario())
    with db() as session:
        stopped = session.get(AgentTask, task.id)
        notices = session.exec(select(Message).where(Message.band_message_id.startswith(f"budget:{task.id}:"))).all()
        audit = session.exec(select(AuditEvent).where(AuditEvent.task_id == task.id)).all()
    assert stopped.status == "cancelled" and stopped.progress.startswith(f"Stopped: {agent.name} used")
    assert len(notices) == 1 and "retry the task" in notices[0].content
    assert [e.action for e in audit] == ["task_stopped"]

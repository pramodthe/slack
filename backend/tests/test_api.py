from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.main import app


def _signup(client: TestClient, username: str, name: str) -> dict:
    response = client.post(
        "/api/auth/signup", json={"username": username, "display_name": name, "password": "secret123"}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_humans_chat_while_band_is_not_configured():
    with TestClient(app) as client:
        alice = _signup(client, "alice", "Alice")
        bob = _signup(client, "bob", "Bob")
        auth_a = {"Authorization": f"Bearer {alice['token']}"}
        auth_b = {"Authorization": f"Bearer {bob['token']}"}

        boot = client.get("/api/bootstrap", headers=auth_a).json()
        assert boot["band"] == {
            "configured": False,
            "online": False,
            "owner_name": None,
            "owner_handle": None,
            "error": "BAND_USER_API_KEY is not set",
        }
        channel = next(c for c in boot["channels"] if c["name"] == "general")
        assert {alice["user"]["id"], bob["user"]["id"]} <= set(channel["human_ids"])

        response = client.post("/api/channels", json={"name": "Launch Plan"}, headers=auth_a)
        assert response.status_code == 503
        assert "BAND_USER_API_KEY" in response.json()["detail"]

        with client.websocket_connect(f"/ws?token={bob['token']}") as ws:
            posted = client.post(
                f"/api/channels/{channel['id']}/messages",
                json={"content": "hey @Bob, ready?"},
                headers=auth_a,
            ).json()
            assert posted["mentions"] == [{"type": "human", "id": bob["user"]["id"], "name": "Bob"}]
            event = ws.receive_json()
            while event["type"] != "message":
                event = ws.receive_json()
            assert event["message"]["content"] == "hey @Bob, ready?"

        messages = client.get(f"/api/channels/{channel['id']}/messages", headers=auth_b).json()
        assert [m["author_name"] for m in messages] == ["Alice"]

        response = client.post("/api/agents", json={"name": "Writer", "role": "writer"}, headers=auth_a)
        assert response.status_code == 503
        response = client.post("/api/agents", json={"name": "W", "role": "writer"}, headers=auth_a)
        assert response.status_code == 422

        assert client.get("/api/bootstrap").status_code == 401
        time.sleep(0.1)

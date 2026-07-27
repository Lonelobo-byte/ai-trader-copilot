from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_user_can_save_rotate_and_remove_own_openrouter_connection() -> None:
    client = TestClient(app)

    initial = client.get("/ai-connection")
    assert initial.status_code == 200
    assert initial.json()["connected"] is False

    saved = client.put("/ai-connection/openrouter", json={"api_key": "sk-or-v1-example-secret", "model": "openai/gpt-4.1-mini"})
    assert saved.status_code == 200
    body = saved.json()
    assert body["connected"] is True
    assert body["model"] == "openai/gpt-4.1-mini"
    assert body["key_hint"] == "••••cret"
    assert "example-secret" not in saved.text

    updated = client.put("/ai-connection/openrouter", json={"api_key": "", "model": "google/gemini-2.5-flash"})
    assert updated.status_code == 200
    assert updated.json()["model"] == "google/gemini-2.5-flash"
    assert updated.json()["key_hint"] == "••••cret"

    removed = client.delete("/ai-connection/openrouter")
    assert removed.status_code == 204
    assert client.get("/ai-connection").json()["connected"] is False


def test_first_connection_requires_a_key_and_model_is_validated() -> None:
    client = TestClient(app)
    no_key = client.put("/ai-connection/openrouter", json={"api_key": "", "model": "openrouter/free"})
    assert no_key.status_code == 422
    invalid_model = client.put("/ai-connection/openrouter", json={"api_key": "sk-or-v1-example-secret", "model": "invalid model id"})
    assert invalid_model.status_code == 422

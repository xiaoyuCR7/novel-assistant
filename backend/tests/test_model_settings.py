import json

from fastapi.testclient import TestClient

from novel_harness.main import create_app


def config(**extra):
    return dict(
        mode="api",
        base_url="https://example.com/v1",
        model="test-model",
        api_key="test-secret-not-real",
        external_consent=True,
        **extra,
    )


def test_settings_require_consent_and_do_not_return_secrets(client):
    assert client.get("/api/v1/settings/model").json()["mode"] == "demo"
    data = config()
    data["external_consent"] = False
    assert client.put("/api/v1/settings/model", json=data).status_code == 422
    data["external_consent"] = True
    response = client.put("/api/v1/settings/model", json=data)
    assert response.status_code == 200
    assert response.json()["has_api_key"] is True
    assert "test-secret" not in response.text
    assert "api_key" not in response.json()


def test_settings_survive_restart_encrypted_and_key_can_be_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOVEL_AI_PROVIDER", "demo")
    with TestClient(create_app(start_executor=False)) as first:
        assert first.put("/api/v1/settings/model", json=config()).status_code == 200
    saved = tmp_path / ".settings" / "model.json"
    assert "test-secret" not in saved.read_text()
    assert json.loads(saved.read_text())["protected_key"]
    with TestClient(create_app(start_executor=False)) as restarted:
        assert restarted.get("/api/v1/settings/model").json()["has_api_key"]
        response = restarted.put(
            "/api/v1/settings/model", json={"mode": "demo", "clear_api_key": True}
        )
        assert response.status_code == 200
        assert not response.json()["has_api_key"]


def test_settings_reject_cross_origin_and_insecure_endpoint(client):
    assert (
        client.put(
            "/api/v1/settings/model", json=config(), headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    data = config()
    data["base_url"] = "http://example.com/v1"
    assert client.put("/api/v1/settings/model", json=data).status_code == 422


def test_endpoint_change_cannot_reuse_key(client):
    assert client.put("/api/v1/settings/model", json=config()).status_code == 200
    data = config()
    data["api_key"] = ""
    data["base_url"] = "https://another.example/v1"
    assert client.put("/api/v1/settings/model", json=data).status_code == 422


def test_connection_test_demo_uses_no_novel_content(client):
    response = client.post("/api/v1/settings/model/test")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_validation_never_echoes_a_submitted_secret(client):
    data = config()
    data["api_key"] = "fake-sensitive-value-" * 300
    response = client.put("/api/v1/settings/model", json=data)
    assert response.status_code == 422
    assert "fake-sensitive-value" not in response.text


def test_model_settings_accept_same_origin_browser(client):
    response = client.put(
        "/api/v1/settings/model", json=config(), headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 200


def test_settings_accept_configured_loopback_dev_origin(client):
    client.app.state.settings.cors_origins = "http://127.0.0.1:5173"
    response = client.put(
        "/api/v1/settings/model", json=config(), headers={"Origin": "http://127.0.0.1:5173"}
    )
    assert response.status_code == 200


def test_settings_accept_default_frontend_dev_origin(client):
    response = client.put(
        "/api/v1/settings/model",
        json=config(),
        headers={"Origin": "http://127.0.0.1:4173"},
    )
    assert response.status_code == 200

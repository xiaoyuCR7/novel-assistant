import httpx
import pytest

from novel_harness.ai.base import AITextRequest, ProviderError
from novel_harness.ai.compatible_provider import CompatibleProvider
from novel_harness.ai.local_provider import LocalProvider


def test_untrusted_origin_cannot_complete_or_restore(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    url = base + f"/chapters/{seeded_chapter}"
    client.put(url, json={"content": "safe", "revision": client.get(url).json()['revision']})
    response = client.post(
        base + f"/chapters/{seeded_chapter}/complete",
        headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
        content="",
    )
    assert response.status_code == 403
    assert client.get(base + f"/chapters/{seeded_chapter}/summary").json() is None


def test_untrusted_host_cannot_list_projects(client):
    assert client.get("/api/v1/projects", headers={"Host": "evil.example"}).status_code == 403


def test_malformed_origin_is_rejected_without_server_error(client):
    assert client.get("/api/v1/projects", headers={"Origin": "http://["}).status_code == 403


def test_invalid_stored_mode_type_is_recoverable(client, tmp_path):
    import json

    path = tmp_path / ".settings/model.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mode": [],
                "base_url": "",
                "model": "",
                "protected_key": "",
                "external_consent": False,
            }
        ),
        encoding="utf-8",
    )
    assert client.get("/api/v1/settings/model").status_code == 503


def test_compatible_connection_failure_retries_once():
    calls = []

    def unavailable(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("not sent", request=request)
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
        )

    provider = CompatibleProvider(
        "https://api.example.com/v1", "test", "test-key", transport=httpx.MockTransport(unavailable)
    )
    assert (
        provider.generate_text(
            AITextRequest(task="chat", developer_instruction="", user_prompt="")
        ).text
        == "ok"
    )
    assert len(calls) == 2


def test_corrupt_settings_require_explicit_repair_and_preserve_backup(client, tmp_path):
    path = tmp_path / ".settings/model.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    assert client.get("/api/v1/settings/model").status_code == 503
    assert client.put("/api/v1/settings/model", json={"mode": "demo"}).status_code == 422
    repaired = client.put(
        "/api/v1/settings/model",
        json={"mode": "demo", "clear_api_key": True, "repair_config": True},
    )
    assert repaired.status_code == 200
    assert repaired.json()["mode"] == "demo"
    backups = list(path.parent.glob("model.corrupt-*.json"))
    assert len(backups) == 1 and backups[0].read_text() == "{broken"


def test_local_truncated_response_is_not_an_acceptable_candidate():
    provider = LocalProvider(
        "http://127.0.0.1:11434",
        "test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"done": True, "done_reason": "length", "message": {"content": "incomplete"}},
            )
        ),
    )
    with pytest.raises(ProviderError):
        provider.generate_text(
            AITextRequest(task="draft", developer_instruction="write", user_prompt="test")
        )


def test_connection_failure_retries_once_but_read_timeout_is_not_retried():
    calls = []

    def unavailable(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("not sent", request=request)
        return httpx.Response(200, json={"message": {"content": "ok"}})

    provider = LocalProvider(
        "http://127.0.0.1:11434", "test", transport=httpx.MockTransport(unavailable)
    )
    assert (
        provider.generate_text(
            AITextRequest(task="chat", developer_instruction="", user_prompt="")
        ).text
        == "ok"
    )
    assert len(calls) == 2
    calls.clear()

    def uncertain(request):
        calls.append(request)
        raise httpx.ReadTimeout("may have generated", request=request)

    provider.transport = httpx.MockTransport(uncertain)
    with pytest.raises(ProviderError):
        provider.generate_text(AITextRequest(task="chat", developer_instruction="", user_prompt=""))
    assert len(calls) == 1


@pytest.mark.parametrize("provider_type", ["local", "api"])
def test_provider_response_size_is_bounded(provider_type, monkeypatch):
    from novel_harness.ai import compatible_provider, local_provider

    module = local_provider if provider_type == "local" else compatible_provider
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 128, raising=False)
    payload = (
        {"message": {"content": "x" * 1000}}
        if provider_type == "local"
        else {"choices": [{"message": {"content": "x" * 1000}}]}
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    provider = (
        LocalProvider("http://127.0.0.1:11434", "test", transport=transport)
        if provider_type == "local"
        else CompatibleProvider(
            "https://api.example.com/v1", "test", "fake-key", transport=transport
        )
    )
    with pytest.raises(ProviderError):
        provider.generate_text(AITextRequest(task="chat", developer_instruction="", user_prompt=""))

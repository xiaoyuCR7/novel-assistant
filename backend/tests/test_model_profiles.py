"""Named model configurations: local storage only, never a model request."""

import json
from uuid import uuid4

from fastapi.testclient import TestClient

from novel_harness.main import create_app
from novel_harness.services.secret_store import reveal

BASE = "/api/v1/settings/model"


def current(client):
    response = client.get(BASE)
    assert response.status_code == 200
    return response.json()


def create_profile(client, name="日常写作", identifier=None):
    body = {
        "id": identifier or str(uuid4()),
        "name": name,
        "expected_config_revision": current(client)["config_revision"],
    }
    response = client.post(BASE + "/profiles", json=body)
    assert response.status_code == 201, response.text
    return response.json(), body


def save_api(client, url, model, key):
    response = client.put(
        BASE,
        json={
            "mode": "api",
            "base_url": url,
            "model": model,
            "api_key": key,
            "external_consent": True,
            "context_capacity": 65536,
            "output_token_budget": 2048,
            "thinking_mode": "disabled",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_save_and_apply_are_local_keep_identity_stable_and_never_expose_key(client, monkeypatch):
    settings = client.app.state.model_settings
    monkeypatch.setattr(
        settings,
        "provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Saving and applying must never create a provider")
        ),
    )
    api = save_api(client, "https://one.example/v1", "writer", "fake-profile-secret-one")
    identity = settings.identity()
    profile, body = create_profile(client)
    assert profile["config"]["context_capacity"] == 65536
    assert profile["config"]["has_api_key"] is True
    assert profile["is_current"] is True
    assert settings.identity() == identity
    assert "config_revision" not in identity
    assert "fake-profile-secret" not in json.dumps(profile)
    assert "protected_key" not in json.dumps(profile)
    assert client.post(BASE + "/profiles", json=body).json()["id"] == profile["id"]
    stored = settings.path.with_name("model-profiles.json").read_text(encoding="utf-8")
    assert "fake-profile-secret" not in stored
    client.put(BASE, json={"mode": "demo", "clear_api_key": True})
    apply_body = {
        "expected_revision": profile["revision"],
        "expected_config_revision": current(client)["config_revision"],
    }
    applied = client.post(BASE + "/profiles/" + profile["id"] + "/apply", json=apply_body)
    assert applied.status_code == 200, applied.text
    assert applied.json()["model"] == "writer"
    assert applied.json()["config_revision"] == api["config_revision"]
    assert settings.identity() == identity
    assert (
        client.post(BASE + "/profiles/" + profile["id"] + "/apply", json=apply_body).status_code
        == 200
    )
    assert reveal(settings._read()["protected_key"]) == "fake-profile-secret-one"


def test_unsaved_default_demo_roundtrip_marks_current_and_replays_original_apply(
    client, monkeypatch
):
    settings = client.app.state.model_settings
    assert not settings.path.exists()
    initial = current(client)
    identity = settings.identity()
    assert initial["mode"] == "demo"
    assert "config_revision" not in identity
    profile, _ = create_profile(client, "未保存过的默认演示")
    profile_path = settings.path.with_name("model-profiles.json")
    stored_text = profile_path.read_text(encoding="utf-8")
    stored = json.loads(stored_text)
    assert type(stored["items"][0]["config"]["deadline_seconds"]) is int
    assert profile["is_current"] is True
    changed = client.put(BASE, json={"mode": "demo", "context_capacity": 65536})
    assert changed.status_code == 200
    assert client.get(BASE + "/profiles").json()["items"][0]["is_current"] is False
    body = {
        "expected_revision": profile["revision"],
        "expected_config_revision": changed.json()["config_revision"],
    }
    url = BASE + "/profiles/" + profile["id"] + "/apply"
    applied = client.post(url, json=body)
    assert applied.status_code == 200, applied.text
    assert applied.json()["context_capacity"] == initial["context_capacity"]
    assert settings.identity() == identity
    assert client.get(BASE + "/profiles").json()["items"][0]["is_current"] is True
    assert applied.json()["config_revision"] == initial["config_revision"]

    def must_not_resave(*_args, **_kwargs):
        raise AssertionError("Replaying the original application must be read-only")

    monkeypatch.setattr(settings, "save", must_not_resave)
    replayed = client.post(url, json=body)
    assert replayed.status_code == 200, replayed.text
    assert replayed.json() == applied.json()
    assert profile_path.read_text(encoding="utf-8") == stored_text
    assert settings.spending.report()["entries"] == []


def test_switching_endpoints_uses_only_the_selected_profiles_own_key(client):
    settings = client.app.state.model_settings
    save_api(client, "https://one.example/v1", "writer", "fake-key-one")
    first, _ = create_profile(client, "一号服务")
    save_api(client, "https://two.example/v1", "editor", "fake-key-two")
    second, _ = create_profile(client, "二号服务")
    response = client.post(
        BASE + "/profiles/" + first["id"] + "/apply",
        json={
            "expected_revision": first["revision"],
            "expected_config_revision": current(client)["config_revision"],
        },
    )
    assert response.status_code == 200
    assert response.json()["base_url"] == "https://one.example/v1"
    assert reveal(settings._read()["protected_key"]) == "fake-key-one"
    assert second["id"] != first["id"]


def test_stale_capture_apply_rename_and_delete_do_not_overwrite_newer_changes(client):
    initial = current(client)
    profile, _ = create_profile(client)
    client.put(BASE, json={"mode": "demo", "context_capacity": 65536})
    assert (
        client.post(
            BASE + "/profiles",
            json={
                "id": str(uuid4()),
                "name": "过期输入",
                "expected_config_revision": initial["config_revision"],
            },
        ).status_code
        == 409
    )
    applied = client.post(
        BASE + "/profiles/" + profile["id"] + "/apply",
        json={
            "expected_revision": profile["revision"],
            "expected_config_revision": initial["config_revision"],
        },
    )
    assert applied.status_code == 409
    assert current(client)["context_capacity"] == 65536
    renamed = client.patch(
        BASE + "/profiles/" + profile["id"],
        json={
            "name": "重新命名",
            "expected_revision": profile["revision"],
        },
    )
    assert renamed.status_code == 200
    assert (
        client.patch(
            BASE + "/profiles/" + profile["id"],
            json={
                "name": "过期改名",
                "expected_revision": profile["revision"],
            },
        ).status_code
        == 409
    )
    assert (
        client.delete(
            BASE + "/profiles/" + profile["id"],
            params={
                "expected_revision": profile["revision"],
            },
        ).status_code
        == 409
    )
    assert (
        client.delete(
            BASE + "/profiles/" + profile["id"],
            params={
                "expected_revision": renamed.json()["revision"],
            },
        ).status_code
        == 200
    )
    assert current(client)["context_capacity"] == 65536
    assert client.get(BASE + "/profiles").json()["items"] == []


def test_profiles_survive_restart_and_reject_cross_origin_and_duplicate_names(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NOVEL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOVEL_AI_PROVIDER", "demo")
    with TestClient(create_app(start_executor=False)) as first:
        profile, body = create_profile(first, "我的方案")
        assert first.post(BASE + "/profiles", json={**body, "id": str(uuid4())}).status_code == 409
        assert (
            first.post(
                BASE + "/profiles",
                json={**body, "id": str(uuid4()), "name": "跨域"},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
    with TestClient(create_app(start_executor=False)) as restarted:
        assert restarted.get(BASE + "/profiles").json()["items"][0]["id"] == profile["id"]


def test_decryption_failure_does_not_borrow_current_api_key(client, monkeypatch):
    save_api(client, "https://first.example/v1", "writer", "fake-first-key")
    profile, _ = create_profile(client)
    save_api(client, "https://second.example/v1", "editor", "fake-second-key")
    saved = current(client)

    def cannot_decrypt(_cipher):
        raise ValueError("sensitive-internal-ciphertext-must-not-be-echoed")

    monkeypatch.setattr("novel_harness.services.model_profiles.reveal", cannot_decrypt)
    response = client.post(
        BASE + "/profiles/" + profile["id"] + "/apply",
        json={
            "expected_revision": profile["revision"],
            "expected_config_revision": saved["config_revision"],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "MODEL_PROFILE_KEY_UNAVAILABLE"
    assert "sensitive-internal" not in response.text
    assert current(client) == saved


def test_corrupt_profile_file_and_failed_atomic_write_preserve_existing_settings(
    client, monkeypatch
):
    profile, _ = create_profile(client)
    settings = client.app.state.model_settings
    path = settings.path.with_name("model-profiles.json")
    original = path.read_bytes()
    saved = current(client)

    def fail_replace(_source, _target):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("novel_harness.services.model_profiles.os.replace", fail_replace)
    response = client.patch(
        BASE + "/profiles/" + profile["id"],
        json={
            "expected_revision": profile["revision"],
            "name": "不能丢失原方案",
        },
    )
    assert response.status_code == 503
    assert path.read_bytes() == original
    assert current(client) == saved
    path.write_text("{broken private data", encoding="utf-8")
    assert client.get(BASE + "/profiles").status_code == 503
    assert (
        client.post(
            BASE + "/profiles",
            json={
                "id": str(uuid4()),
                "name": "不得覆盖损坏文件",
                "expected_config_revision": saved["config_revision"],
            },
        ).status_code
        == 503
    )
    assert path.read_text(encoding="utf-8") == "{broken private data"
    assert current(client) == saved


def test_ordinary_model_save_supports_revision_fencing_without_changing_old_clients(client):
    previous = current(client)
    changed = client.put(
        BASE,
        json={
            "mode": "demo",
            "context_capacity": 65536,
            "expected_config_revision": previous["config_revision"],
        },
    )
    assert changed.status_code == 200
    stale = client.put(
        BASE,
        json={
            "mode": "demo",
            "context_capacity": 32768,
            "expected_config_revision": previous["config_revision"],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "MODEL_CONFIG_CHANGED"
    assert current(client) == changed.json()
    assert client.put(BASE, json={"mode": "demo", "context_capacity": 32768}).status_code == 200


def test_applying_original_profile_restores_exact_durable_job_identity_without_running_it(
    client, project
):
    from novel_harness.db.models import AIJob

    save_api(client, "https://first.example/v1", "writer", "fake-original-key")
    profile, _ = create_profile(client)
    jobs = f"/api/v1/projects/{project['id']}/ai/jobs"
    receipt = client.post(
        jobs,
        headers={"Idempotency-Key": "profile-identity-job"},
        json={
            "project_id": project["id"],
            "task_type": "chat",
            "instructions": "offline fixture",
        },
    )
    assert receipt.status_code == 202
    job = receipt.json()
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        session.get(AIJob, job["id"]).status = "failed"
    client.put(BASE, json={"mode": "demo", "clear_api_key": True})
    body = {"expected_control_revision": job["control_revision"], "confirm_unknown": False}
    blocked = client.post(
        jobs + "/" + job["id"] + "/resume", json=body, headers={"Idempotency-Key": "wrong-profile"}
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "PROVIDER_CHANGED"
    applied = client.post(
        BASE + "/profiles/" + profile["id"] + "/apply",
        json={
            "expected_revision": profile["revision"],
            "expected_config_revision": current(client)["config_revision"],
        },
    )
    assert applied.status_code == 200
    resumed = client.post(
        jobs + "/" + job["id"] + "/resume",
        json=body,
        headers={"Idempotency-Key": "original-profile"},
    )
    assert resumed.status_code == 202
    assert resumed.json()["status"] == "queued"
    assert client.app.state.model_settings.spending.report()["entries"] == []

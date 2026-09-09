import io
import zipfile

from job_helpers import create_chapter_version

from novel_harness.ai.base import ProviderExecutionError


class FailingImageProvider:
    name = "failing"

    def generate_image(self, request):
        raise ProviderExecutionError("图像服务暂时不可用")


def test_asset_generation_progress_and_project_export(client, project, seeded_chapter):
    character = client.post(
        f"/api/v1/projects/{project['id']}/entities",
        json={
            "kind": "character",
            "name": "林渡",
            "summary": "雾城邮差",
            "profile": {"appearance": "黑色旧制服，左手戴皮手套"},
            "state": {},
        },
    ).json()

    asset_response = client.post(
        f"/api/v1/projects/{project['id']}/assets/generate",
        json={
            "project_id": project["id"],
            "entity_id": character["id"],
            "kind": "character",
            "prompt": "雾城邮差林渡的全身角色设定图",
            "size": "1024x1024",
        },
    )
    assert asset_response.status_code == 201
    asset = asset_response.json()
    assert asset["status"] == "ready"
    assert asset["relative_path"].startswith("assets/")
    assert not asset["relative_path"].startswith("/")
    asset_path = (
        client.app.state.vault_registry.require(project["id"]).root / asset["relative_path"]
    )
    assert asset_path.exists()
    original_bytes = asset_path.read_bytes()
    displayed = client.get(f"/api/v1/projects/{project['id']}/assets/{asset['id']}/file")
    assert displayed.status_code == 200
    assert displayed.headers["content-type"] == "image/png"
    assert displayed.content == original_bytes

    original_provider = client.app.state.ai_provider
    client.app.state.ai_provider = FailingImageProvider()
    failed = client.post(
        f"/api/v1/projects/{project['id']}/assets/generate",
        json={
            "project_id": project["id"],
            "entity_id": character["id"],
            "kind": "character",
            "prompt": "失败也不能覆盖旧素材",
        },
    )
    client.app.state.ai_provider = original_provider
    assert failed.status_code == 502
    assert asset_path.read_bytes() == original_bytes

    version = create_chapter_version(
        client,
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}/versions",
        json={"content": "雾落下来。林渡推开邮局的门。", "source": "manual"},
    )
    assert version.status_code == 201

    progress = client.get(f"/api/v1/projects/{project['id']}/progress")
    assert progress.status_code == 200
    stats = progress.json()
    assert stats["current_words"] == version.json()["word_count"]
    assert stats["target_words"] == project["target_words"]
    assert 0 < stats["completion_ratio"] < 1
    assert stats["chapter_count"] == 1

    exported = client.get(f"/api/v1/projects/{project['id']}/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        names = set(archive.namelist())
        assert "project.json" in names
        assert "assets-manifest.json" in names
        assert "versions.json" in names
        assert any(name.startswith("manuscript/") and name.endswith(".md") for name in names)
        project_json = archive.read("project.json").decode("utf-8")
        assert "雾城来信" in project_json

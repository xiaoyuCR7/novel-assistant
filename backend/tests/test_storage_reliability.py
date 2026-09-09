import io
import sqlite3
import zipfile
from contextlib import closing
from datetime import timedelta

from job_helpers import complete_summary, save_chapter

from novel_harness.db.base import utc_now
from novel_harness.db.models import Idea


def test_stale_document_cannot_overwrite_new_save(client, project, seeded_chapter):
    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    original = client.get(url).json()
    payload = {"content": "先保存的新正文", "contract": {}, "revision": original["revision"]}
    assert save_chapter(client, url, json=payload).status_code == 200
    payload["content"] = "过期窗口的正文"
    result = save_chapter(client, url, json=payload)
    assert result.status_code == 409
    assert result.json()["detail"]["current"]["content"] == "先保存的新正文"
    assert client.get(url).json()["content"] == "先保存的新正文"


def test_trash_chapter_removes_summary_and_restore_does_not_revive_stale_summary(
    client, project, seeded_chapter
):
    base = f"/api/v1/projects/{project['id']}"
    url = base + f"/chapters/{seeded_chapter}"
    save_chapter(client, url, json={"content": "独有银杏钥匙", "contract": {}})
    summary = complete_summary(client, url).json()
    client.delete(base + f"/library/node/{seeded_chapter}")
    assert not any(
        i["type"] == "summary"
        for i in client.get(base + "/library/search", params={"q": "独有银杏钥匙"}).json()["items"]
    )
    vault = client.app.state.vault_registry.require(project["id"])
    ledger = vault.root / "rag/chapter-continuity-ledger.md"
    assert "独有银杏钥匙" not in ledger.read_text(encoding="utf-8")
    client.post(base + f"/trash/node/{seeded_chapter}/restore")
    client.delete(base + f"/library/summary/{summary['id']}")
    save_chapter(client, url, json={"content": "钥匙已经融化", "contract": {}})
    client.post(base + f"/trash/summary/{summary['id']}/restore")
    assert client.get(url + "/summary").json()["status"] != "valid"
    assert "独有银杏钥匙" not in ledger.read_text(encoding="utf-8")


def test_expired_trash_is_collected_on_use(client, project):
    base = f"/api/v1/projects/{project['id']}"
    item = client.post(base + "/library/idea", json={"title": "到期素材"}).json()
    client.delete(base + f"/library/idea/{item['id']}")
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        idea = session.get(Idea, item["id"], execution_options={"include_deleted": True})
        idea.purge_after = utc_now() - timedelta(days=1)
    assert client.get(base + "/trash").json() == []


def test_backup_contains_database_ledger_and_materials(client, project, seeded_chapter, tmp_path):
    base = f"/api/v1/projects/{project['id']}"
    client.post(base + "/library/entity", json={"title": "备份人物", "content": "完整设定"})
    save_chapter(client, base + f"/chapters/{seeded_chapter}", json={"content": "完整正文"})
    complete_summary(client, base + f"/chapters/{seeded_chapter}")
    archive = zipfile.ZipFile(io.BytesIO(client.get(base + "/export").content))
    assert "project.db" in archive.namelist()
    assert "rag/chapter-continuity-ledger.md" in archive.namelist()
    snapshot = tmp_path / "snapshot.db"
    snapshot.write_bytes(archive.read("project.db"))
    with closing(sqlite3.connect(snapshot)) as connection, connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT name FROM entities").fetchone()[0] == "备份人物"
        assert connection.execute("SELECT count(*) FROM chapter_summaries").fetchone()[0] == 1

from datetime import timedelta

import pytest

from novel_harness.db.base import utc_now
from novel_harness.db.models import Asset
from novel_harness.services.library import purge_item


def test_asset_file_is_not_deleted_by_a_rolled_back_purge(client, project):
    base = f"/api/v1/projects/{project['id']}"
    asset = client.post(
        base + "/assets/generate",
        json={"project_id": project["id"], "kind": "scene", "prompt": "测试图"},
    ).json()
    client.delete(base + f"/library/asset/{asset['id']}")
    vault = client.app.state.vault_registry.require(project["id"])
    path = vault.root / asset["relative_path"]
    with vault.database.session_scope() as session:
        item = session.get(Asset, asset["id"], execution_options={"include_deleted": True})
        item.purge_after = utc_now() - timedelta(days=1)
    with pytest.raises(RuntimeError), vault.database.session_scope() as session:
        purge_item(session, "asset", asset["id"])
        raise RuntimeError("rollback the surrounding operation")
    assert path.is_file()
    with vault.database.session_scope() as session:
        assert (
            session.get(Asset, asset["id"], execution_options={"include_deleted": True}) is not None
        )
    client.get(base + "/trash")
    assert not path.exists()

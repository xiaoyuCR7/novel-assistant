"""Compare legacy and bounded library reads on synthetic data only.

Run from backend with PYTHONPATH=src. Timings are individual observations,
not p95 measurements. No author settings, model calls or production Vaults.
"""

import json
import os
from tempfile import TemporaryDirectory
from time import perf_counter


def main():
    with TemporaryDirectory(prefix="novel-library-metrics-") as directory:
        os.environ.update(
            NOVEL_DATA_DIR=directory, NOVEL_AI_PROVIDER="demo", NOVEL_LOCAL_EMBEDDING_MODEL=""
        )
        from fastapi.testclient import TestClient
        from sqlalchemy import event

        from novel_harness.db.models import Idea
        from novel_harness.main import create_app

        with TestClient(create_app(start_executor=False)) as client:
            created = client.post("/api/v1/projects", json={"title": "Library benchmark"})
            created.raise_for_status()
            project_id = created.json()["id"]
            base = f"/api/v1/projects/{project_id}"
            database = client.app.state.vault_registry.require(project_id).database
            body = "Synthetic scene. " * 750
            with database.session_scope() as session:
                session.add_all([
                    Idea(project_id=project_id, title=f"Material {i:04d}", content=body)
                    for i in range(1000)
                ])

            statements = []

            def observe(_conn, _cursor, sql, _params, _context, _many):
                statements.append(sql)

            def measure(path):
                statements.clear()
                started = perf_counter()
                response = client.get(base + path)
                duration_ms = round((perf_counter() - started) * 1000, 2)
                response.raise_for_status()
                return response.json(), {
                    "sql_count": len(statements),
                    "response_bytes": len(response.content),
                    "request_ms": duration_ms,
                }

            event.listen(database.engine, "before_cursor_execute", observe)
            try:
                old, legacy = measure("/library")
                first, bounded = measure("/library/page?limit=50")
                assert len(old) == first["total"] == first["counts"]["idea"] == 1000
                assert len(first["items"]) == 50
                assert all(
                    "content" not in item and "record" not in item for item in first["items"]
                )
                second, next_page = measure("/library/page?limit=50&cursor=" + first["next_cursor"])
                assert len(second["items"]) == 50
                assert not ({item["id"] for item in first["items"]}
                            & {item["id"] for item in second["items"]})
                detail, full_detail = measure("/library/idea/" + first["items"][0]["id"])
                assert detail["content"] == body
                _, workspace = measure("/workspace")
                navigation, light_workspace = measure("/workspace/navigation")
                assert "ideas" not in navigation
                old_search, legacy_search = measure("/library/search?q=Synthetic&category=idea")
                light_search, bounded_search = measure(
                    "/library/search?q=Synthetic&category=idea&lightweight=true"
                )
                assert [item["id"] for item in old_search["items"]] == [
                    item["id"] for item in light_search["items"]
                ]
                assert light_search["items"] and all(
                    "record" not in item and "content" not in item for item in light_search["items"]
                )
            finally:
                event.remove(database.engine, "before_cursor_execute", observe)
            print(json.dumps({
                "fixture": {"materials": 1000, "chars_per_material": len(body)},
                "legacy_library": {"rows": len(old), **legacy},
                "library_page_50": bounded,
                "library_next_50": next_page,
                "one_full_detail": full_detail,
                "legacy_workspace": workspace,
                "navigation": light_workspace,
                "legacy_search": {"rows": len(old_search["items"]), **legacy_search},
                "light_search": {"rows": len(light_search["items"]), **bounded_search},
                "model_calls": 0,
                "note": "Single synthetic run, not production p95; HTTP includes serialization.",
            }, indent=2))


if __name__ == "__main__":
    main()

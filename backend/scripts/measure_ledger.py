"""Isolated synthetic ledger metrics; never opens author data or calls a model."""

import hashlib
import json
import os
from tempfile import TemporaryDirectory
from time import perf_counter


def main():
    with TemporaryDirectory(prefix="novel-ledger-metrics-") as directory:
        os.environ.update(
            NOVEL_DATA_DIR=directory, NOVEL_AI_PROVIDER="demo", NOVEL_LOCAL_EMBEDDING_MODEL=""
        )
        from fastapi.testclient import TestClient
        from sqlalchemy import event

        from novel_harness.db.models import (
            ChapterDocument,
            ChapterSummary,
            ChapterVersion,
            StoryNode,
        )
        from novel_harness.main import create_app
        from novel_harness.services.search_index import rebuild_index

        results = []
        with TestClient(create_app(start_executor=False)) as client:
            for count in (1, 10, 50):
                response = client.post("/api/v1/projects", json={"title": f"Ledger {count}"})
                response.raise_for_status()
                project_id = response.json()["id"]
                database = client.app.state.vault_registry.require(project_id).database
                with database.job_session_scope() as session:
                    session.info["index_ready"] = False
                    nodes = [StoryNode(project_id=project_id, kind="chapter", title=f"C{i}",
                                       order_index=i, status="completed") for i in range(count)]
                    session.add_all(nodes)
                    session.flush()
                    versions = [ChapterVersion(project_id=project_id, chapter_id=n.id,
                                               content=f"original {i}", source="manual",
                                               word_count=10) for i, n in enumerate(nodes)]
                    session.add_all(versions)
                    session.flush()
                    session.add_all([
                        ChapterDocument(project_id=project_id, chapter_id=n.id,
                                        current_version_id=v.id, content=v.content)
                        for n, v in zip(nodes, versions, strict=True)
                    ])
                    session.add_all([
                        ChapterSummary(project_id=project_id, chapter_id=n.id, version_id=v.id,
                                       title=n.title,
                                       content_hash=hashlib.sha256(v.content.encode()).hexdigest(),
                                       recap="recap", details={})
                        for n, v in zip(nodes, versions, strict=True)
                    ])
                    session.flush()
                    rebuild_index(session)
                    first = nodes[0].id
                statements = []

                def observe(_conn, _cursor, sql, _params, _context, _many, statements=statements):
                    statements.append(sql)

                event.listen(database.engine, "before_cursor_execute", observe)
                try:
                    started = perf_counter()
                    response = client.put(
                        f"/api/v1/projects/{project_id}/chapters/{first}",
                        json={"content": "changed draft", "contract": {}, "revision": 1},
                    )
                    elapsed = round((perf_counter() - started) * 1000, 2)
                    response.raise_for_status()
                    assert response.json()["content"] == "changed draft"
                    assert response.json()["status"] == "drafting"
                finally:
                    event.remove(database.engine, "before_cursor_execute", observe)
                results.append({"completed_chapters": count, "sql_count": len(statements),
                                "projection_inserts": sum("INSERT INTO search_documents" in sql
                                                          for sql in statements),
                                "request_ms": elapsed})
        print(json.dumps({
            "results": results, "model_calls": 0,
            "note": "Single synthetic HTTP request per corpus, not production p95.",
        }, indent=2))


if __name__ == "__main__":
    main()

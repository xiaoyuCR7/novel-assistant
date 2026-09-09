"""Repeatable synthetic history/RAG measurements; no author Vault or network model."""

import json
import os
from tempfile import TemporaryDirectory
from time import perf_counter


def main():
    with TemporaryDirectory(prefix="novel-metrics-") as directory:
        os.environ.update(
            NOVEL_DATA_DIR=directory, NOVEL_AI_PROVIDER="demo", NOVEL_LOCAL_EMBEDDING_MODEL=""
        )
        from fastapi.testclient import TestClient
        from sqlalchemy import event

        from novel_harness.db.models import AIJob, Idea
        from novel_harness.main import create_app
        from novel_harness.services.retrieval import search

        with TestClient(create_app(start_executor=False)) as client:
            project = client.post("/api/v1/projects", json={"title": "Synthetic benchmark"}).json()
            base = f"/api/v1/projects/{project['id']}"
            database = client.app.state.vault_registry.require(project["id"]).database
            with database.session_scope() as session:
                for i in range(100):
                    session.add(
                        AIJob(
                            project_id=project["id"],
                            task_type="chat",
                            status="succeeded",
                            instructions=f"turn{i}",
                            context_snapshot={"reference": "x" * 20000},
                            result={"reply": "r" * 3000},
                            prompt_version="legacy",
                        )
                    )
                session.add_all(
                    [
                        Idea(project_id=project["id"], title=f"quiet{i}", content="quiet")
                        for i in range(200)
                    ]
                )
                session.add(
                    Idea(
                        project_id=project["id"],
                        title="large",
                        content="平静的一天。" * 700 + "needle" + "另一段日常。" * 700,
                    )
                )
            statements = []

            def observe(_conn, _cursor, sql, _params, _context, _many):
                statements.append(sql)

            event.listen(database.engine, "before_cursor_execute", observe)

            def request(path):
                statements.clear()
                started = perf_counter()
                response = client.get(base + path)
                response.raise_for_status()
                return response.json(), {
                    "sql_count": len(statements),
                    "bytes": len(response.content),
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                }

            old, legacy = request("/ai/jobs")
            page, first = request("/ai/jobs/page?limit=50")
            _, second = request("/ai/jobs/page?limit=50&before=" + page["next_cursor"])
            statements.clear()
            started = perf_counter()
            with database.session_scope() as session:
                hits = search(session, "needle")["items"]
            retrieval = {
                "sql_count": len(statements),
                "returned_sources": len(hits),
                "fragment_chars": len(hits[0]["content"]),
                "source_chars": len(hits[0]["record"]["content"]),
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            }
            event.remove(database.engine, "before_cursor_execute", observe)
            print(
                json.dumps(
                    {
                        "fixture": {"jobs": 100, "materials": 201},
                        "legacy": {"rows": len(old), **legacy},
                        "summary_page_50": first,
                        "summary_next_50": second,
                        "retrieval": retrieval,
                        "model_calls": 0,
                        "note": "Single synthetic run, not production p95.",
                    },
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()

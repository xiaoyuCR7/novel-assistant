"""Subprocess fault harness. Receives only pytest-owned temporary paths."""

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

root, boundary = Path(sys.argv[1]), sys.argv[2]
os.environ["NOVEL_DATA_DIR"] = str(root)
os.environ["NOVEL_AI_PROVIDER"] = "demo"
os.environ["NOVEL_LOCAL_EMBEDDING_MODEL"] = ""

from novel_harness.ai.demo import DemoProvider  # noqa: E402
from novel_harness.main import create_app  # noqa: E402
from novel_harness.services import chapter_summaries  # noqa: E402
from novel_harness.services.job_store import JobStore  # noqa: E402


class Counted(DemoProvider):
    def record(self, task):
        with closing(sqlite3.connect(root / "fake-provider-calls.db")) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS calls (task TEXT)")
            connection.execute("INSERT INTO calls VALUES (?)", (task,))

    def generate_text(self, request):
        self.record(request.task)
        return super().generate_text(request)

    def generate_structured(self, request, schema):
        self.record(request.task)
        return super().generate_structured(request, schema)


dispatch = JobStore.dispatch
finish = JobStore.finish_attempt
publish = JobStore.publish
ledger = chapter_summaries.write_ledger


def crash_dispatch(self, *args):
    if boundary == "before_dispatch":
        os._exit(31)
    dispatch(self, *args)
    if boundary == "after_dispatch":
        os._exit(31)


def crash_finish(self, *args, **kwargs):
    finish(self, *args, **kwargs)
    if boundary == "after_output":
        os._exit(31)


def crash_publish(self, *args, **kwargs):
    publish(self, *args, **kwargs)
    if boundary == "after_summary_publish" and kwargs.get("terminal") is False:
        os._exit(31)


def crash_ledger(session):
    ledger(session)
    if boundary == "after_ledger_replace":
        os._exit(31)


JobStore.dispatch = crash_dispatch
JobStore.finish_attempt = crash_finish
JobStore.publish = crash_publish
chapter_summaries.write_ledger = crash_ledger
with TestClient(create_app(start_executor=False)) as client:
    client.app.state.ai_provider = Counted()
    client.app.state.job_executor.run_once()

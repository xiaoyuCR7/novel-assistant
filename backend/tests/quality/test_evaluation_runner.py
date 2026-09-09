"""Synthetic-only runner tests; no actual model connections are authorized here."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "evaluate_quality.py"


def runner():
    assert SCRIPT.is_file(), "The quality evaluation command has not been implemented."
    spec = importlib.util.spec_from_file_location("quality_evaluation", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_evaluation_is_deterministic_offline_and_reports_limits(monkeypatch, capsys):
    evaluate = runner()
    monkeypatch.setenv("NOVEL_DATA_DIR", "DO_NOT_READ_AUTHOR_VAULT")
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_SECRET_DO_NOT_LOG")

    def forbidden_provider(*_args, **_kwargs):
        pytest.fail("The default evaluation must never create a model provider.")

    monkeypatch.setattr(evaluate, "LocalProvider", forbidden_provider)
    assert evaluate.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "deterministic"
    assert report["model_calls"] == 0
    assert report["metrics"]["failed"] == 0
    assert {row["category"] for row in report["cases"]} >= {
        "facts",
        "chronology",
        "knowledge_boundaries",
        "plot_obligations",
        "candidate_provenance",
        "injection_boundary",
        "recall",
    }
    assert report["literary_quality"] == "not_evaluated"
    assert report["real_model"]["status"] == "not_run"
    assert report["recall"]["recall_at_3"] == 1.0
    assert "SYNTHETIC_SECRET" not in json.dumps(report)
    assert evaluate.run_deterministic() == evaluate.run_deterministic()


def test_long_memory_evaluation_is_isolated_offline_and_bounded(monkeypatch, capsys):
    evaluate = runner()
    monkeypatch.setenv("NOVEL_DATA_DIR", "DO_NOT_READ_AUTHOR_VAULT")
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_SECRET_DO_NOT_LOG")

    def forbidden_provider(*_args, **_kwargs):
        pytest.fail("The long-memory evaluation must never create a model provider.")

    monkeypatch.setattr(evaluate, "LocalProvider", forbidden_provider)
    assert evaluate.main(["--long-memory"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    scale = report["long_memory"]

    assert report["mode"] == "deterministic_and_long_memory"
    assert report["model_calls"] == 0
    assert scale["corpus"]["chapters"] == 300
    assert scale["corpus"]["manuscript_characters"] >= 400_000
    assert scale["corpus"]["valid_summaries"] == 299
    assert scale["corpus"]["search_documents"] >= 900
    assert scale["metrics"]["failed"] == 0
    assert scale["context"]["over_budget"] is False
    assert scale["context"]["estimated_tokens"] <= scale["context"]["token_budget"]
    assert {row["id"] for row in scale["cases"] if row["passed"]} >= {
        "distant_confirmed_memory_recalled",
        "pending_memory_excluded",
        "cross_vault_memory_excluded",
        "chapter_scoped_entity_state",
        "context_budget_respected",
    }
    assert scale["timings_ms"].keys() >= {"seed", "search", "context"}
    assert "SYNTHETIC_SECRET" not in output


def test_runner_reports_real_constraint_failures_not_a_fixed_success(monkeypatch):
    evaluate = runner()
    monkeypatch.setattr(evaluate.ContinuityChecker, "check", lambda *_args: [])
    report = evaluate.run_deterministic()
    assert report["metrics"]["failed"] >= 4
    assert any(
        row["category"] == "knowledge_boundaries" and not row["passed"] for row in report["cases"]
    )


@pytest.mark.parametrize(
    "args,code",
    [
        (
            ["--endpoint", "http://127.0.0.1:11434", "--model", "synthetic"],
            "REAL_MODEL_NOT_AUTHORIZED",
        ),
        (["--real-model"], "EXPLICIT_LOCAL_CONFIG_REQUIRED"),
        (
            ["--real-model", "--endpoint", "https://external.example", "--model", "synthetic"],
            "INVALID_LOCAL_CONFIGURATION",
        ),
    ],
)
def test_model_evaluation_requires_explicit_authorization_and_local_configuration(
    args, code, capsys
):
    evaluate = runner()
    assert evaluate.main(args) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["error_code"] == code
    assert report["model_calls"] == 0


def test_real_mode_records_actual_calls_and_usage_without_response_text(monkeypatch, capsys):
    evaluate = runner()
    actual_provider = evaluate.LocalProvider
    sent = []

    def handle(request):
        payload = json.loads(request.content)
        sent.append(payload)
        assert request.url.path == "/api/chat"
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "done": True,
                "message": {
                    "content": json.dumps(
                        {
                            "answer": "wrong",
                            "source_id": "forged",
                            "quote": "SECRET_RAW_MODEL_OUTPUT",
                        }
                    )
                },
                "prompt_eval_count": 12,
                "eval_count": 4,
            },
        )

    monkeypatch.setattr(
        evaluate,
        "LocalProvider",
        lambda endpoint, model, **kwargs: actual_provider(
            endpoint, model, transport=httpx.MockTransport(handle), **kwargs
        ),
    )
    assert (
        evaluate.main(
            [
                "--real-model",
                "--endpoint",
                "http://127.0.0.1:11434",
                "--model",
                "synthetic-model",
                "--output-tokens",
                "256",
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["real_model"]["status"] == "completed"
    assert report["real_model"]["configuration"]["model"] == "synthetic-model"
    assert report["model_calls"] == len(sent) == 6
    assert report["real_model"]["usage"]["input_tokens"] == 72
    assert report["real_model"]["usage"]["output_tokens"] == 24
    assert report["real_model"]["failed"] == 6
    assert report["real_model"]["usage_sources"] == ["provider"]
    assert all(payload["options"]["num_predict"] == 256 for payload in sent)
    assert "SECRET_RAW_MODEL_OUTPUT" not in output
    assert all("expected_answer" not in json.dumps(payload) for payload in sent)


def test_unknown_real_model_result_stops_the_evaluation_without_retry(monkeypatch, capsys):
    evaluate = runner()
    actual_provider = evaluate.LocalProvider
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("SECRET_TRANSPORT_DETAIL")

    monkeypatch.setattr(
        evaluate,
        "LocalProvider",
        lambda endpoint, model, **kwargs: actual_provider(
            endpoint, model, transport=httpx.MockTransport(fail), **kwargs
        ),
    )
    assert (
        evaluate.main(
            ["--real-model", "--endpoint", "http://127.0.0.1:11434", "--model", "synthetic-model"]
        )
        == 1
    )
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["real_model"]["status"] == "stopped"
    assert report["model_calls"] == len(calls) == 1
    assert report["real_model"]["cases"][0]["error_code"] == "PROVIDER_RESULT_UNKNOWN"
    assert report["real_model"]["skipped"] == 5
    assert "SECRET_TRANSPORT_DETAIL" not in output


def test_model_capacity_failure_returns_json_without_attempting_a_connection(capsys):
    evaluate = runner()
    assert (
        evaluate.main(
            [
                "--real-model",
                "--endpoint",
                "http://127.0.0.1:11434",
                "--model",
                "synthetic-model",
                "--context-capacity",
                "1024",
                "--output-tokens",
                "1024",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["model_calls"] == 0
    assert report["real_model"]["status"] == "stopped"
    assert report["real_model"]["cases"][0]["error_code"] == "INPUT_BUDGET_EXCEEDED"

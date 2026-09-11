from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from novel_harness.ai.base import AITextRequest, ProviderExecutionError
from novel_harness.ai.compatible_provider import CompatibleProvider
from novel_harness.services.spending import SpendingLedger, SpendingSettings

IDENTITY = {"mode": "api", "base_url": "https://example.com/v1", "model": "test"}


def configured(tmp_path, limit="1"):
    ledger = SpendingLedger(tmp_path / "spending.db")
    ledger.configure(
        SpendingSettings.model_validate(
            {
                "revision": 0,
                "global_limit": limit,
                "prices": [{**IDENTITY, "input_per_million": "1", "output_per_million": "2"}],
            }
        )
    )
    return ledger


def request():
    return AITextRequest(
        task="conversation_summary",
        developer_instruction="Summarize.",
        user_prompt="private content",
        output_token_budget=256,
    )


def test_usage_and_unknown_survive_restart_without_sensitive_text(tmp_path):
    ledger = configured(tmp_path)
    meter = ledger.meter(
        IDENTITY, project_id="project", job_id="job", stage="conversation.compact.1"
    )
    first = meter.reserve(request())
    meter.sent(first)
    meter.settle(first, {"input_tokens": 100, "output_tokens": 20}, "provider")
    second = meter.reserve(request())
    meter.sent(second)  # A process crash has no callback. Reservation must survive.
    restored = SpendingLedger(tmp_path / "spending.db").report("project")
    assert restored["totals"]["settled_cny"] == "0.000140"
    assert restored["totals"]["unresolved_count"] == 1
    assert restored["entries"][0]["stage"] == "conversation.compact.1"
    assert "private content" not in str(restored)


def test_atomic_reservations_cannot_exceed_limit(tmp_path):
    ledger = configured(tmp_path, "0.0007")

    def send(_):
        try:
            return ledger.meter(IDENTITY).reserve(request())
        except ProviderExecutionError as exc:
            assert exc.code == "SPENDING_LIMIT_REACHED"
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(send, range(4)))
    assert len([result for result in results if result]) == 1


def test_limit_requires_known_price_and_old_unknowns_block(tmp_path):
    ledger = SpendingLedger(tmp_path / "spending.db")
    ledger.meter(IDENTITY).reserve(request())
    ledger.configure(SpendingSettings.model_validate({"revision": 0, "global_limit": "1"}))
    with pytest.raises(ProviderExecutionError, match="单价"):
        ledger.meter(IDENTITY).reserve(request())


def test_connect_retry_is_released_and_each_actual_send_is_recorded(tmp_path):
    ledger = configured(tmp_path)
    count = 0

    def handle(req):
        nonlocal count
        count += 1
        if count == 1:
            raise httpx.ConnectError("not sent", request=req)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    provider = CompatibleProvider(
        IDENTITY["base_url"], "test", "secret", transport=httpx.MockTransport(handle)
    )
    provider.spending = ledger.meter(IDENTITY)
    assert provider.generate_text(request()).text == "OK"
    report = ledger.report()
    assert {row["status"] for row in report["entries"]} == {"not_sent", "settled"}
    assert report["totals"]["settled_cny"] == "0.000014"


def test_limit_stops_before_checkpoint_and_network(tmp_path):
    ledger = configured(tmp_path, "0")

    class Observer:
        def before_send(self):
            pytest.fail("No checkpoint should be marked sent")

    provider = CompatibleProvider(
        IDENTITY["base_url"],
        "test",
        "secret",
        attempt_observer=Observer(),
        transport=httpx.MockTransport(lambda req: pytest.fail("No network")),
    )
    provider.spending = ledger.meter(IDENTITY)
    with pytest.raises(ProviderExecutionError) as error:
        provider.generate_text(request())
    assert error.value.code == "SPENDING_LIMIT_REACHED"


def test_truncated_output_usage_is_still_charged(tmp_path):
    ledger = configured(tmp_path)
    provider = CompatibleProvider(
        IDENTITY["base_url"],
        "test",
        "secret",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "length", "message": {"content": "cut"}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 256},
                },
            )
        ),
    )
    provider.spending = ledger.meter(IDENTITY)
    with pytest.raises(ProviderExecutionError):
        provider.generate_text(request())
    assert ledger.report()["totals"]["settled_cny"] == "0.000524"


def test_project_cap_does_not_block_another_project(tmp_path):
    ledger = configured(tmp_path)
    ledger.configure(
        SpendingSettings.model_validate({**ledger.settings(), "project_limits": {"one": "0"}})
    )
    with pytest.raises(ProviderExecutionError):
        ledger.meter(IDENTITY, project_id="one").reserve(request())
    assert ledger.meter(IDENTITY, project_id="two").reserve(request())


def test_unknown_request_can_be_reconciled_only_after_execution_ends(tmp_path):
    ledger = configured(tmp_path)
    meter = ledger.meter(IDENTITY)
    entry = meter.reserve(request())
    meter.sent(entry)
    with pytest.raises(ValueError):
        ledger.reconcile(entry, "0", "not yet known")
    meter.unknown(entry)
    ledger.reconcile(entry, "0.01", "provider bill checked")
    assert ledger.report()["totals"]["committed_cny"] == "0.010000"
    assert ledger.report()["totals"]["unresolved_count"] == 0
    with pytest.raises(ValueError):
        ledger.reconcile(entry, "0", "cannot erase audit")


def test_unpriced_success_requires_reconciliation_before_enabling_cap(tmp_path):
    ledger = SpendingLedger(tmp_path / "spending.db")
    meter = ledger.meter(IDENTITY)
    entry = meter.reserve(request())
    meter.settle(entry, {"input_tokens": 10, "output_tokens": 1}, "provider")
    assert ledger.report()["entries"][0]["can_reconcile"]
    ledger.reconcile(entry, "0.001", "bill")
    assert ledger.report()["totals"]["unpriced_count"] == 0


def test_settings_api_revision_and_origin_protection(client):
    response = client.get("/api/v1/settings/spending")
    assert response.status_code == 200
    settings = response.json()["settings"]
    assert (
        client.put("/api/v1/settings/spending", json={**settings, "global_limit": "2"}).status_code
        == 200
    )
    assert client.put("/api/v1/settings/spending", json=settings).status_code == 409
    assert (
        client.put(
            "/api/v1/settings/spending", json=settings, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/api/v1/settings/spending", json={**settings, "global_limit": "NaN"}
        ).status_code
        == 422
    )


def test_connection_test_also_has_spending_guard(client):
    assert (
        client.put(
            "/api/v1/settings/model",
            json={**IDENTITY, "api_key": "fake-key", "external_consent": True},
        ).status_code
        == 200
    )
    settings = client.get("/api/v1/settings/spending").json()["settings"]
    assert (
        client.put(
            "/api/v1/settings/spending",
            json={
                **settings,
                "global_limit": "0",
                "prices": [{**IDENTITY, "input_per_million": "1", "output_per_million": "2"}],
            },
        ).status_code
        == 200
    )
    response = client.post("/api/v1/settings/model/test")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "SPENDING_LIMIT_REACHED"

"""Independent spending review, no network or author data."""

from novel_harness.ai.base import AITextRequest
from novel_harness.services.spending import SpendingLedger


def test_pending_reconciliation_remains_reachable_after_recent_100_are_resolved(tmp_path):
    ledger = SpendingLedger(tmp_path / 'review-spending.db')
    meter = ledger.meter({'base_url': 'https://example.com/v1', 'model': 'offline-fixture'})
    request = AITextRequest(task='chat', user_prompt='fixture', developer_instruction='fixture')
    for _ in range(101):
        identifier = meter.reserve(request)
        meter.unknown(identifier)
    visible = ledger.report()['entries']
    assert len(visible) == 100
    for entry in visible:
        ledger.reconcile(entry['id'], '0', 'Offline fixture: verified no charge')
    report = ledger.report()
    assert report['totals']['unresolved_count'] == 1
    assert any(entry['can_reconcile'] for entry in report['entries'])

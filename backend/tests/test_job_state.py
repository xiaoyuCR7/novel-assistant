import importlib

import pytest


@pytest.mark.parametrize(
    ("stage", "cancel", "expected"),
    [
        ("prepared", False, "continue"),
        ("succeeded", False, "reuse"),
        ("skipped", False, "reuse"),
        ("dispatched", False, "pause_unknown"),
        ("result_unknown", False, "pause_unknown"),
        (None, False, "pause_unknown"),
        ("future_status", False, "pause_unknown"),
        ("failed", False, "pause_failed"),
        ("dispatched", True, "cancel"),
    ],
)
def test_recovery_never_guesses_that_an_unknown_request_was_not_sent(stage, cancel, expected):
    state = importlib.import_module("novel_harness.services.job_state")
    assert state.recovery_action(stage, cancel) == expected


def test_command_hash_orders_keys_without_changing_author_text():
    state = importlib.import_module("novel_harness.services.job_state")
    assert state.command_hash({"a": 1, "b": "正文"}) == state.command_hash({"b": "正文", "a": 1})
    assert state.command_hash({"b": "正文"}) != state.command_hash({"b": "正文 "})
    assert state.command_hash({"instructions": ""}) != state.command_hash({"instructions": None})
    assert state.command_hash({"instructions": "一"}) != state.command_hash({"instructions": "二"})

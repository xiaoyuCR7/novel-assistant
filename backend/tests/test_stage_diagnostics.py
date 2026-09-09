from types import SimpleNamespace

from novel_harness.services.job_stages import StageRunner


def test_stage_diagnostics_retain_metrics_without_logging_content(caplog, tmp_path):
    import logging

    saved = []
    store = SimpleNamespace(
        database=SimpleNamespace(path=tmp_path),
        completed=lambda *args: None,
        begin_attempt=lambda *args: 1,
        finish_attempt=lambda *args: saved.append(args[-1]),
    )
    runner = StageRunner(store, SimpleNamespace(job_id="job"))
    with caplog.at_level(logging.INFO, logger="novel_harness.stages"):
        runner.run(
            "chat",
            {},
            lambda _: {
                "text": "PRIVATE_CONTENT",
                "usage": {"output_tokens": 3},
                "usage_source": "provider",
            },
            lambda value: value,
        )
    assert saved[0]["execution"]["duration_ms"] >= 0
    assert len(saved[0]["execution"]["diagnostic_id"]) == 32
    assert "PRIVATE_CONTENT" not in caplog.text
    logger = logging.getLogger('novel_harness.stages')
    assert saved[0]["execution"]["diagnostic_id"] in caplog.text, (
        logger.disabled, logger.level, logger.propagate, logger.parent.name,
        logging.root.manager.disable, caplog.handler.level,
        [type(handler).__name__ for handler in logging.root.handlers],
    )
    assert runner.provider_metadata["stages"]["chat"]["usage"] == {"output_tokens": 3}

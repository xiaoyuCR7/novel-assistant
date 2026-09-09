import sys
from importlib import import_module
from subprocess import run

import pytest


def test_lock_is_process_exclusive_and_releases(tmp_path):
    lock_type = import_module("novel_harness.services.executor_lock").ExecutorLock
    script = (
        "from pathlib import Path; from novel_harness.services.executor_lock import "
        "ExecutorLock; import sys; lock=ExecutorLock(Path(sys.argv[1])); lock.__enter__()"
    )
    with lock_type(tmp_path):
        with pytest.raises(RuntimeError):
            with lock_type(tmp_path):
                pass
        assert (
            run([sys.executable, "-c", script, str(tmp_path)], capture_output=True).returncode != 0
        )
    assert run([sys.executable, "-c", script, str(tmp_path)], capture_output=True).returncode == 0

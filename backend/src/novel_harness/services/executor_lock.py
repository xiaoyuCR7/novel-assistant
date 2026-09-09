"""OS-owned data-directory exclusion; process exit releases the lock."""

import os


class ExecutorLock:
    def __init__(self, root):
        self.root = root
        self.file = None

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        handle = (self.root / ".executor.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("DATA_DIRECTORY_ALREADY_OWNED") from None
        self.file = handle
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()
            self.file = None

"""Disposable read-only preview; never a checkpoint, version or RAG source."""

from collections import OrderedDict
from itertools import count
from threading import Lock
from uuid import uuid4


class PreviewRegistry:
    def __init__(self, max_chars=160000, max_jobs=32):
        self.max_chars, self.max_jobs = max_chars, max_jobs
        self.lock, self.rows, self.sequence = Lock(), OrderedDict(), count(1)

    def begin(self, vault, job, stage):
        with self.lock:
            token = uuid4().hex
            self.rows[(vault, job)] = {'token': token, 'stage': stage, 'text': '',
                                      'sequence': next(self.sequence), 'truncated': False}
            self.rows.move_to_end((vault, job))
            while len(self.rows) > self.max_jobs:
                self.rows.popitem(last=False)
            return token

    def append(self, vault, job, token, delta):
        with self.lock:
            row = self.rows.get((vault, job))
            if row is None or row['token'] != token:
                return
            text = row['text'] + delta
            row.update(text=text[:self.max_chars], sequence=next(self.sequence),
                       truncated=row['truncated'] or len(text) > self.max_chars)

    def read(self, vault, job):
        with self.lock:
            row = self.rows.get((vault, job))
            return {key: value for key, value in row.items() if key != 'token'} if row else {
                'stage': None, 'text': '', 'sequence': 0, 'truncated': False,
            }

    def drop(self, vault, job):
        with self.lock:
            self.rows.pop((vault, job), None)


previews = PreviewRegistry()

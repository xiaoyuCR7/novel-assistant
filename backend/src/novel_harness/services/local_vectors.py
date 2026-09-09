"""Per-Vault, incremental chunk vectors; source hashes reject edits and tombstones."""

import hashlib
import json
import math
import sqlite3
from contextlib import closing, contextmanager

from sqlalchemy import bindparam, text

from novel_harness.services.chunks import STRATEGY


def fingerprint(data):
    return hashlib.sha256(data.encode()).hexdigest()


class LocalVectorIndex:
    def __init__(self, root, provider):
        self.root = root
        self.path = root / "rag/vectors/vectors.db"
        self.provider = provider
        self.model_identity = json.dumps({
            'provider': getattr(provider, 'name', 'local'),
            'endpoint': getattr(provider, 'endpoint', ''), 'model': provider.model,
        }, sort_keys=True)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS vectors ("
                    "key TEXT, chunk INTEGER, source_hash TEXT, model TEXT, embedding TEXT, "
                    "PRIMARY KEY(key,chunk))"
                )
                existing = {row[1] for row in db.execute("PRAGMA table_info(vectors)")}
                for name in ("chunk_key", "chunk_hash", "strategy"):
                    if name not in existing:
                        db.execute(f"ALTER TABLE vectors ADD COLUMN {name} TEXT")
                yield db
        finally:
            db.close()

    def rebuild(self, session):
        current = (
            session.execute(
                text(
                    "SELECT chunk_key,document_key,ordinal,source_hash,"
                    "chunk_hash,strategy FROM search_chunks"
                )
            )
            .mappings()
            .all()
        )
        with self.connect() as db:
            cached = {
                row[0]: row[1:]
                for row in db.execute(
                    "SELECT chunk_key,chunk_hash,model,strategy,embedding FROM vectors"
                )
            }
        changed = [
            row["chunk_key"]
            for row in current
            if not (
                row["chunk_key"] in cached
                and cached[row["chunk_key"]][:3]
                == (row["chunk_hash"], self.model_identity, row["strategy"])
            )
        ]
        embeddings = {}
        if changed:
            statement = text(
                "SELECT c.chunk_key,c.body,d.title FROM search_chunks c "
                "JOIN search_documents d ON d.key=c.document_key "
                "WHERE c.chunk_key IN :keys"
            ).bindparams(bindparam("keys", expanding=True))
            bodies = session.execute(statement, {"keys": changed}).all()
            # No vector-cache write transaction is held across a provider call.
            for key, body, title in bodies:
                embeddings[key] = json.dumps(self.provider.embed(title + "\n" + body))
        values = [
            (
                row["document_key"],
                row["ordinal"],
                row["source_hash"],
                self.model_identity,
                embeddings[row["chunk_key"]]
                if row["chunk_key"] in embeddings
                else cached[row["chunk_key"]][3],
                row["chunk_key"],
                row["chunk_hash"],
                row["strategy"],
            )
            for row in current
        ]
        with self.connect() as db:
            db.execute("DELETE FROM vectors")
            db.executemany(
                "INSERT INTO vectors "
                "(key,chunk,source_hash,model,embedding,chunk_key,chunk_hash,strategy) "
                "VALUES (?,?,?,?,?,?,?,?)",
                values,
            )
        return len({row["document_key"] for row in current})

    def _current_hashes(self):
        with closing(sqlite3.connect(self.root / "project.db")) as source:
            return dict(
                source.execute(
                    "SELECT chunk_key,source_hash FROM search_chunks WHERE strategy=?", (STRATEGY,)
                )
            )

    def search_chunks(self, query, limit, *, eligible_keys=None):
        if not self.path.is_file() or not query.strip():
            return []
        hashes = self._current_hashes()
        if eligible_keys is not None:
            hashes = {key: digest for key, digest in hashes.items() if key in eligible_keys}
        with self.connect() as db:
            rows = db.execute(
                "SELECT chunk_key,source_hash,embedding FROM vectors WHERE model=? AND strategy=?",
                (self.model_identity, STRATEGY),
            ).fetchall()
        rows = [row for row in rows if hashes.get(row[0]) == row[1]]
        if not rows:
            return []
        vector = self.provider.embed(query)
        norm = math.sqrt(sum(value * value for value in vector))
        scores = []
        for key, _, encoded in rows:
            other = json.loads(encoded)
            if len(other) != len(vector):
                continue
            denominator = norm * math.sqrt(sum(value * value for value in other))
            similarity = (
                sum(a * b for a, b in zip(vector, other, strict=True)) / denominator
                if denominator
                else 0
            )
            if similarity > 0:
                scores.append((key, similarity))
        diverse, seen = [], set()
        for key, score in sorted(scores, key=lambda pair: (-pair[1], pair[0])):
            document_key = key.rsplit(':', 2)[0]
            if document_key not in seen:
                seen.add(document_key)
                diverse.append((key, score))
        return diverse[:max(0, limit)]

    def search(self, query, limit):
        """Historical document-key API used by older callers."""
        scores = {}
        for key, score in self.search_chunks(query, 2**31 - 1):
            document_key = key.rsplit(":", 2)[0]
            scores[document_key] = max(scores.get(document_key, 0), score)
        return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)[:limit]

    def status(self):
        if not self.path.is_file():
            return "needs_rebuild"
        with self.connect() as db:
            indexed = dict(
                db.execute(
                    "SELECT chunk_key,source_hash FROM vectors WHERE model=? AND strategy=?",
                    (self.model_identity, STRATEGY),
                )
            )
        return "ready" if indexed == self._current_hashes() else "needs_rebuild"

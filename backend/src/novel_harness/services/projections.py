"""Transactional ledger outbox; only committed SQL may reach the filesystem."""

import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


def queue_ledger(session):
    session.execute(text("INSERT OR IGNORE INTO pending_projections(kind) VALUES ('ledger')"))
    session.info['ledger_queued'] = True


def drain_ledger(database):
    from novel_harness.services.chapter_summaries import finish_ledger, write_ledger

    # The short writer lock serializes snapshot/render/ack with new domain writes.
    # No model or embedding requests are made here.
    with database._sessions() as session:
        session.info.update(vault_root=database.path.parent, projecting_ledger=True)
        try:
            session.execute(text('BEGIN IMMEDIATE'))
            if not session.scalar(text("SELECT kind FROM pending_projections WHERE kind='ledger'")):
                session.rollback()
                return True
            write_ledger(session)
            session.info['index_ready'] = True
            finish_ledger(session)
            session.flush()
            session.execute(text("DELETE FROM pending_projections WHERE kind='ledger'"))
            session.commit()
            return True
        except (OSError, SQLAlchemyError):
            session.rollback()
            logging.getLogger(__name__).warning('LEDGER_PROJECTION_PENDING')
            return False

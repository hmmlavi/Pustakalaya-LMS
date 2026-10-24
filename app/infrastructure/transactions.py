"""Unit of Work and transactional boundary helpers."""

from __future__ import annotations

from contextlib import contextmanager
import logging
from typing import Iterator

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.exceptions import CirculationError

logger = logging.getLogger("pustakalaya.transactions")


class UnitOfWork:
    """Coordinates an atomic database unit of work.

    When `write=True`, acquires a SQLite `BEGIN IMMEDIATE` write transaction
    before any reads execute inside the block, preventing check-then-act races
    between concurrent circulation or reservation requests.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        write: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._write = write
        self.session: Session | None = None
        self._committed = False

    def __enter__(self) -> Session:
        self.session = self._session_factory()
        if self._write:
            self.session.connection(execution_options={"sqlite_immediate": True})
        return self.session

    def commit(self) -> None:
        if self.session is None:
            raise RuntimeError("UnitOfWork session is not active.")
        self.session.commit()
        self._committed = True

    def rollback(self) -> None:
        if self.session is not None:
            self.session.rollback()

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        assert self.session is not None
        try:
            if exc_type is not None:
                self.session.rollback()
                if isinstance(exc_val, OperationalError) and "database is locked" in str(
                    exc_val
                ).lower():
                    logger.warning(
                        "SQLite lock contention encountered; transaction rolled back."
                    )
                    raise CirculationError(
                        "Another circulation operation is currently modifying this record. Please try again."
                    ) from exc_val
                return False
            if not self._committed:
                self.session.commit()
                self._committed = True
            self.session.expunge_all()
            return False
        except Exception:
            self.session.rollback()
            raise
        finally:
            self.session.close()


@contextmanager
def read_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Open a read-only session that detaches loaded objects cleanly before rolling back."""
    session = session_factory()
    try:
        yield session
        session.expunge_all()
    finally:
        session.rollback()
        session.close()


@contextmanager
def write_transaction(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Open an atomic write transaction backed by UnitOfWork(write=True)."""
    with UnitOfWork(session_factory, write=True) as session:
        yield session

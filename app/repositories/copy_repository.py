"""Repository for physical BookCopy inventory persistence and atomic state updates."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, joinedload

from app.domain.enums import CopyStatus
from app.domain.value_objects import utc_now
from app.models import Book, BookCopy
from app.repositories.base import Page, escape_like, paginate_statement


class CopyRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, copy_id: int) -> BookCopy | None:
        stmt = (
            select(BookCopy)
            .options(
                joinedload(BookCopy.book).selectinload(Book.authors),
                joinedload(BookCopy.owning_branch),
            )
            .where(BookCopy.id == copy_id)
        )
        return self.session.scalar(stmt)

    def get_by_accession(self, accession_number: str) -> BookCopy | None:
        cleaned = accession_number.strip().upper()
        stmt = (
            select(BookCopy)
            .options(
                joinedload(BookCopy.book).selectinload(Book.authors),
                joinedload(BookCopy.owning_branch),
            )
            .where(func.upper(BookCopy.accession_number) == cleaned)
        )
        return self.session.scalar(stmt)

    def list_for_book(self, book_id: int) -> Sequence[BookCopy]:
        stmt = (
            select(BookCopy)
            .options(joinedload(BookCopy.owning_branch))
            .where(BookCopy.book_id == book_id)
            .order_by(BookCopy.accession_number.asc())
        )
        return list(self.session.scalars(stmt).all())

    def count_available_for_book(
        self, book_id: int, *, branch_id: int | None = None
    ) -> int:
        stmt = select(func.count(BookCopy.id)).where(
            BookCopy.book_id == book_id,
            BookCopy.status == CopyStatus.AVAILABLE.value,
        )
        if branch_id is not None:
            stmt = stmt.where(BookCopy.owning_branch_id == branch_id)
        return int(self.session.scalar(stmt) or 0)

    def count_circulatable_for_book(
        self, book_id: int, *, branch_id: int | None = None
    ) -> int:
        stmt = select(func.count(BookCopy.id)).where(
            BookCopy.book_id == book_id,
            BookCopy.status.notin_(
                [CopyStatus.WITHDRAWN.value, CopyStatus.LOST.value]
            ),
        )
        if branch_id is not None:
            stmt = stmt.where(BookCopy.owning_branch_id == branch_id)
        return int(self.session.scalar(stmt) or 0)

    def conditional_transition_status(
        self,
        copy_id: int,
        *,
        expected_statuses: Sequence[CopyStatus],
        new_status: CopyStatus,
    ) -> bool:
        """Atomically transition status only if the copy currently matches expected_statuses.

        Provides an extra concurrency defense alongside SQLite BEGIN IMMEDIATE
        and partial unique indexes.
        """
        expected_vals = [s.value for s in expected_statuses]
        stmt = (
            update(BookCopy)
            .where(BookCopy.id == copy_id, BookCopy.status.in_(expected_vals))
            .values(status=new_status.value, updated_at=utc_now())
        )
        result = self.session.execute(stmt)
        if result.rowcount == 1:
            copy_obj = self.session.get(BookCopy, copy_id)
            if copy_obj is not None:
                self.session.refresh(copy_obj)
            return True
        return False

    def search(
        self,
        *,
        query: str | None = None,
        branch_id: int | None = None,
        status: str | None = None,
        book_id: int | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> Page[BookCopy]:
        stmt = (
            select(BookCopy)
            .join(Book, Book.id == BookCopy.book_id)
            .options(
                joinedload(BookCopy.book).selectinload(Book.authors),
                joinedload(BookCopy.owning_branch),
            )
        )
        if query and query.strip():
            like_q = f"%{escape_like(query.strip().lower())}%"
            isbn_q = f"%{escape_like(query.strip().replace('-', '').upper())}%"
            stmt = stmt.where(
                or_(
                    func.lower(BookCopy.accession_number).like(like_q, escape="\\"),
                    func.lower(Book.title).like(like_q, escape="\\"),
                    Book.isbn.like(isbn_q, escape="\\"),
                )
            )
        if branch_id is not None:
            stmt = stmt.where(BookCopy.owning_branch_id == branch_id)
        if status and status.strip():
            stmt = stmt.where(BookCopy.status == status.strip().upper())
        if book_id is not None:
            stmt = stmt.where(BookCopy.book_id == book_id)

        stmt = stmt.order_by(BookCopy.accession_number.asc())
        return paginate_statement(self.session, stmt, page=page, per_page=per_page)

    def add(self, copy: BookCopy) -> BookCopy:
        self.session.add(copy)
        self.session.flush()
        return copy

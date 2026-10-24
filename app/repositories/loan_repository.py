"""Repository for Loan circulation records, overdue queries, and conditional renewals."""

from __future__ import annotations

from datetime import date
from typing import Sequence
import uuid

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, joinedload

from app.domain.enums import LoanStatus
from app.domain.value_objects import utc_now
from app.models import Book, BookCopy, Loan, Member
from app.repositories.base import Page, escape_like, paginate_statement


class LoanRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _eager_options():
        return (
            joinedload(Loan.member).joinedload(Member.tier),
            joinedload(Loan.copy).joinedload(BookCopy.book).selectinload(Book.authors),
            joinedload(Loan.copy).joinedload(BookCopy.owning_branch),
            joinedload(Loan.issuing_branch),
            joinedload(Loan.returned_branch),
            joinedload(Loan.fine),
        )

    def get_by_id(self, loan_id: int) -> Loan | None:
        stmt = select(Loan).options(*self._eager_options()).where(Loan.id == loan_id)
        return self.session.scalar(stmt)

    def get_by_loan_number(self, loan_number: str) -> Loan | None:
        stmt = (
            select(Loan)
            .options(*self._eager_options())
            .where(func.upper(Loan.loan_number) == loan_number.strip().upper())
        )
        return self.session.scalar(stmt)

    def get_by_source_reference(self, source_ref: str) -> Loan | None:
        stmt = select(Loan).where(Loan.source_reference == source_ref)
        return self.session.scalar(stmt)

    def get_active_loan_for_copy(self, copy_id: int) -> Loan | None:
        stmt = (
            select(Loan)
            .options(*self._eager_options())
            .where(Loan.copy_id == copy_id, Loan.status == LoanStatus.ACTIVE.value)
        )
        return self.session.scalar(stmt)

    def member_has_active_loan_for_book(self, member_id: int, book_id: int) -> bool:
        stmt = (
            select(func.count(Loan.id))
            .join(BookCopy, BookCopy.id == Loan.copy_id)
            .where(
                Loan.member_id == member_id,
                Loan.status == LoanStatus.ACTIVE.value,
                BookCopy.book_id == book_id,
            )
        )
        return int(self.session.scalar(stmt) or 0) > 0

    def count_active_for_member(self, member_id: int) -> int:
        stmt = select(func.count(Loan.id)).where(
            Loan.member_id == member_id,
            Loan.status == LoanStatus.ACTIVE.value,
        )
        return int(self.session.scalar(stmt) or 0)

    def count_active_overdue_for_member(self, member_id: int, as_of_date: date) -> int:
        stmt = select(func.count(Loan.id)).where(
            Loan.member_id == member_id,
            Loan.status == LoanStatus.ACTIVE.value,
            Loan.due_date < as_of_date,
        )
        return int(self.session.scalar(stmt) or 0)

    def list_for_member(
        self, member_id: int, *, active_only: bool = False, limit: int = 100
    ) -> Sequence[Loan]:
        stmt = (
            select(Loan)
            .options(*self._eager_options())
            .where(Loan.member_id == member_id)
            .order_by(Loan.checked_out_at.desc(), Loan.id.desc())
            .limit(limit)
        )
        if active_only:
            stmt = stmt.where(Loan.status == LoanStatus.ACTIVE.value)
        return list(self.session.scalars(stmt).unique().all())

    def list_for_book(self, book_id: int, *, limit: int = 50) -> Sequence[Loan]:
        stmt = (
            select(Loan)
            .join(BookCopy, BookCopy.id == Loan.copy_id)
            .options(*self._eager_options())
            .where(BookCopy.book_id == book_id)
            .order_by(Loan.checked_out_at.desc(), Loan.id.desc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt).unique().all())

    def conditional_apply_renewal(
        self,
        loan_id: int,
        *,
        expected_renewal_count: int,
        max_renewals: int,
        new_due_date: date,
    ) -> bool:
        """Atomically increment renewal_count and update due_date if state matches."""
        stmt = (
            update(Loan)
            .where(
                Loan.id == loan_id,
                Loan.status == LoanStatus.ACTIVE.value,
                Loan.renewal_count == expected_renewal_count,
                Loan.renewal_count < max_renewals,
            )
            .values(
                renewal_count=expected_renewal_count + 1,
                due_date=new_due_date,
                updated_at=utc_now(),
            )
        )
        result = self.session.execute(stmt)
        if result.rowcount == 1:
            loan_obj = self.session.get(Loan, loan_id)
            if loan_obj is not None:
                self.session.refresh(loan_obj)
            return True
        return False

    def next_loan_number(self, checkout_date: date) -> str:
        token = uuid.uuid4().hex[:8].upper()
        return f"LN-{checkout_date.strftime('%Y%m%d')}-{token}"

    def search(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        branch_id: int | None = None,
        overdue_only: bool = False,
        as_of_date: date | None = None,
        member_id: int | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> Page[Loan]:
        stmt = (
            select(Loan)
            .join(Member, Member.id == Loan.member_id)
            .join(BookCopy, BookCopy.id == Loan.copy_id)
            .join(Book, Book.id == BookCopy.book_id)
            .options(*self._eager_options())
        )
        if query and query.strip():
            like_q = f"%{escape_like(query.strip().lower())}%"
            stmt = stmt.where(
                or_(
                    func.lower(Loan.loan_number).like(like_q, escape="\\"),
                    func.lower(Member.member_code).like(like_q, escape="\\"),
                    func.lower(Member.full_name).like(like_q, escape="\\"),
                    func.lower(BookCopy.accession_number).like(like_q, escape="\\"),
                    func.lower(Book.title).like(like_q, escape="\\"),
                )
            )
        if status and status.strip():
            stmt = stmt.where(Loan.status == status.strip().upper())
        if branch_id is not None:
            stmt = stmt.where(Loan.issuing_branch_id == branch_id)
        if member_id is not None:
            stmt = stmt.where(Loan.member_id == member_id)
        if overdue_only and as_of_date is not None:
            stmt = stmt.where(
                Loan.status == LoanStatus.ACTIVE.value,
                Loan.due_date < as_of_date,
            )

        stmt = stmt.order_by(Loan.checked_out_at.desc(), Loan.id.desc())
        return paginate_statement(self.session, stmt, page=page, per_page=per_page)

    def add(self, loan: Loan) -> Loan:
        self.session.add(loan)
        self.session.flush()
        return loan

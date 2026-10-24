"""Repository for Fine ledger entries, FinePayments, and member outstanding balances."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Sequence
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.domain.enums import FineStatus
from app.domain.value_objects import ZERO_MONEY, quantize_money
from app.models import Book, BookCopy, Fine, FinePayment, Loan, Member
from app.repositories.base import Page, escape_like, paginate_statement


class FineRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _eager_options():
        return (
            joinedload(Fine.member).joinedload(Member.tier),
            joinedload(Fine.loan)
            .joinedload(Loan.copy)
            .joinedload(BookCopy.book)
            .selectinload(Book.authors),
            joinedload(Fine.loan).joinedload(Loan.issuing_branch),
            selectinload(Fine.payments),
        )

    def get_by_id(self, fine_id: int) -> Fine | None:
        stmt = select(Fine).options(*self._eager_options()).where(Fine.id == fine_id)
        return self.session.scalar(stmt)

    def get_by_fine_number(self, fine_number: str) -> Fine | None:
        stmt = (
            select(Fine)
            .options(*self._eager_options())
            .where(func.upper(Fine.fine_number) == fine_number.strip().upper())
        )
        return self.session.scalar(stmt)

    def get_by_loan_id(self, loan_id: int) -> Fine | None:
        stmt = (
            select(Fine).options(*self._eager_options()).where(Fine.loan_id == loan_id)
        )
        return self.session.scalar(stmt)

    def get_by_source_reference(self, source_ref: str) -> Fine | None:
        return self.session.scalar(
            select(Fine).where(Fine.source_reference == source_ref)
        )

    def get_payment_by_idempotency_key(self, key: str) -> FinePayment | None:
        stmt = (
            select(FinePayment)
            .options(joinedload(FinePayment.fine), joinedload(FinePayment.member))
            .where(FinePayment.idempotency_key == key)
        )
        return self.session.scalar(stmt)

    def get_member_outstanding_balance(self, member_id: int) -> Decimal:
        """Compute exact unpaid balance across all non-paid/non-waived fines for a member."""
        stmt = select(Fine).where(
            Fine.member_id == member_id,
            Fine.status.in_(
                [FineStatus.UNPAID.value, FineStatus.PARTIALLY_PAID.value]
            ),
        )
        fines = self.session.scalars(stmt).all()
        total = sum((f.outstanding_balance for f in fines), ZERO_MONEY)
        return quantize_money(total)

    def list_for_member(self, member_id: int) -> Sequence[Fine]:
        stmt = (
            select(Fine)
            .options(*self._eager_options())
            .where(Fine.member_id == member_id)
            .order_by(Fine.assessed_at.desc(), Fine.id.desc())
        )
        return list(self.session.scalars(stmt).unique().all())

    def list_payments_for_member(self, member_id: int) -> Sequence[FinePayment]:
        stmt = (
            select(FinePayment)
            .options(
                joinedload(FinePayment.fine),
                joinedload(FinePayment.branch),
                joinedload(FinePayment.received_by_user),
            )
            .where(FinePayment.member_id == member_id)
            .order_by(FinePayment.paid_at.desc(), FinePayment.id.desc())
        )
        return list(self.session.scalars(stmt).unique().all())

    def next_fine_number(self, ref_date: date) -> str:
        token = uuid.uuid4().hex[:8].upper()
        return f"FN-{ref_date.strftime('%Y%m%d')}-{token}"

    def next_receipt_number(self, ref_date: date) -> str:
        token = uuid.uuid4().hex[:8].upper()
        return f"RCP-{ref_date.strftime('%Y%m%d')}-{token}"

    def search_fines(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        branch_id: int | None = None,
        outstanding_only: bool = False,
        page: int = 1,
        per_page: int = 25,
    ) -> Page[Fine]:
        stmt = (
            select(Fine)
            .join(Member, Member.id == Fine.member_id)
            .join(Loan, Loan.id == Fine.loan_id)
            .options(*self._eager_options())
        )
        if query and query.strip():
            like_q = f"%{escape_like(query.strip().lower())}%"
            stmt = stmt.where(
                or_(
                    func.lower(Fine.fine_number).like(like_q, escape="\\"),
                    func.lower(Member.member_code).like(like_q, escape="\\"),
                    func.lower(Member.full_name).like(like_q, escape="\\"),
                    func.lower(Loan.loan_number).like(like_q, escape="\\"),
                )
            )
        if status and status.strip():
            stmt = stmt.where(Fine.status == status.strip().upper())
        if branch_id is not None:
            stmt = stmt.where(Loan.issuing_branch_id == branch_id)
        if outstanding_only:
            stmt = stmt.where(
                Fine.status.in_(
                    [FineStatus.UNPAID.value, FineStatus.PARTIALLY_PAID.value]
                )
            )
        stmt = stmt.order_by(Fine.assessed_at.desc(), Fine.id.desc())
        return paginate_statement(self.session, stmt, page=page, per_page=per_page)

    def search_payments(
        self,
        *,
        query: str | None = None,
        branch_id: int | None = None,
        payment_method: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> Page[FinePayment]:
        stmt = (
            select(FinePayment)
            .join(Member, Member.id == FinePayment.member_id)
            .join(Fine, Fine.id == FinePayment.fine_id)
            .options(
                joinedload(FinePayment.member),
                joinedload(FinePayment.fine),
                joinedload(FinePayment.branch),
                joinedload(FinePayment.received_by_user),
            )
        )
        if query and query.strip():
            like_q = f"%{escape_like(query.strip().lower())}%"
            stmt = stmt.where(
                or_(
                    func.lower(FinePayment.receipt_number).like(like_q, escape="\\"),
                    func.lower(Fine.fine_number).like(like_q, escape="\\"),
                    func.lower(Member.member_code).like(like_q, escape="\\"),
                    func.lower(Member.full_name).like(like_q, escape="\\"),
                )
            )
        if branch_id is not None:
            stmt = stmt.where(FinePayment.branch_id == branch_id)
        if payment_method and payment_method.strip():
            stmt = stmt.where(
                FinePayment.payment_method == payment_method.strip().upper()
            )
        stmt = stmt.order_by(FinePayment.paid_at.desc(), FinePayment.id.desc())
        return paginate_statement(self.session, stmt, page=page, per_page=per_page)

    def add_fine(self, fine: Fine) -> Fine:
        self.session.add(fine)
        self.session.flush()
        return fine

    def add_payment(self, payment: FinePayment) -> FinePayment:
        self.session.add(payment)
        self.session.flush()
        return payment

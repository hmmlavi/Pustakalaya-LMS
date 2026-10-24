"""Dedicated FineService for holiday-aware fine calculation, ledger entries, and payments."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import AuditEventType, FineStatus, PaymentMethod
from app.domain.exceptions import (
    FineNotFoundError,
    InvalidFinePaymentError,
    LoanNotFoundError,
    MemberNotFoundError,
)
from app.domain.policies import FineBreakdown, HolidayCalendar, PolicyFactory
from app.domain.value_objects import (
    ZERO_MONEY,
    local_today,
    quantize_money,
    to_local_date,
    utc_now,
)
from app.infrastructure.transactions import read_session, write_transaction
from app.models import Fine, FinePayment, Loan
from app.repositories import (
    AuditRepository,
    FineRepository,
    HolidayRepository,
    LoanRepository,
    MemberRepository,
    Page,
)


class FineService:
    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        library_timezone: str = "Asia/Kolkata",
    ) -> None:
        self.session_factory = session_factory
        self.library_timezone = library_timezone

    def calculate_loan_fine_in_session(
        self,
        session: Session,
        loan: Loan,
        *,
        effective_return_date: date,
        calendar: HolidayCalendar | None = None,
    ) -> FineBreakdown:
        hol_calendar = calendar or HolidayRepository(session).build_calendar()
        checkout_local = to_local_date(loan.checked_out_at, self.library_timezone)
        fine_policy = PolicyFactory.create_fine_policy(loan.member.tier, hol_calendar)
        return fine_policy.calculate_fine(
            due_date=loan.due_date,
            effective_return_date=effective_return_date,
            checkout_date=checkout_local,
            branch_id=loan.issuing_branch_id,
        )

    def estimate_loan_fine(
        self, loan_id: int, *, as_of_date: date | None = None
    ) -> FineBreakdown:
        eval_date = as_of_date or local_today(self.library_timezone)
        with read_session(self.session_factory) as session:
            loan = LoanRepository(session).get_by_id(loan_id)
            if loan is None:
                raise LoanNotFoundError(f"Loan #{loan_id} was not found.")
            if loan.returned_at is not None:
                eval_date = to_local_date(loan.returned_at, self.library_timezone)
            return self.calculate_loan_fine_in_session(
                session, loan, effective_return_date=eval_date
            )

    def assess_overdue_fine_in_session(
        self,
        session: Session,
        loan: Loan,
        breakdown: FineBreakdown,
        *,
        assessed_at: datetime,
        reason: str = "Overdue return fine",
        actor_user_id: int | None = None,
        branch_id: int | None = None,
    ) -> Fine | None:
        """Create a Fine ledger entry if assessed_amount > 0 and none exists for loan_id."""
        fine_repo = FineRepository(session)
        existing = fine_repo.get_by_loan_id(loan.id)
        if existing is not None:
            return existing

        if breakdown.assessed_amount <= ZERO_MONEY:
            return None

        local_dt = to_local_date(assessed_at, self.library_timezone)
        fine = Fine(
            fine_number=fine_repo.next_fine_number(local_dt),
            loan_id=loan.id,
            member_id=loan.member_id,
            assessed_amount=breakdown.assessed_amount,
            paid_amount=ZERO_MONEY,
            waived_amount=ZERO_MONEY,
            eligible_overdue_days=breakdown.eligible_overdue_days,
            daily_rate_applied=breakdown.daily_rate,
            status=FineStatus.UNPAID.value,
            reason=reason,
            assessed_at=assessed_at,
        )
        fine_repo.add_fine(fine)
        AuditRepository(session).record(
            event_type=AuditEventType.FINE_ASSESSED,
            entity_type="FINE",
            entity_id=fine.fine_number,
            actor_user_id=actor_user_id,
            branch_id=branch_id or loan.issuing_branch_id,
            summary=(
                f"Assessed fine {fine.fine_number} of INR {fine.assessed_amount} "
                f"on loan {loan.loan_number} ({breakdown.eligible_overdue_days} overdue days)."
            ),
            details={
                "loan_number": loan.loan_number,
                "assessed_amount": str(fine.assessed_amount),
                "eligible_overdue_days": breakdown.eligible_overdue_days,
                "daily_rate": str(breakdown.daily_rate),
                "holidays_excluded": breakdown.holiday_days_excluded,
                "grace_days": breakdown.grace_period_days,
            },
        )
        return fine

    def record_payment(
        self,
        fine_identifier: int | str,
        *,
        amount: Decimal | str | int,
        payment_method: PaymentMethod | str = PaymentMethod.CASH,
        idempotency_key: str | None = None,
        reference_note: str | None = None,
        branch_id: int | None = None,
        actor_user_id: int | None = None,
        paid_at: datetime | None = None,
    ) -> FinePayment:
        """Record a full or partial payment against an assessed Fine atomically."""
        try:
            payment_amount = quantize_money(amount)
        except (TypeError, ValueError) as exc:
            raise InvalidFinePaymentError(str(exc)) from exc

        if payment_amount <= ZERO_MONEY:
            raise InvalidFinePaymentError("Payment amount must be greater than INR 0.00.")

        try:
            method_enum = (
                payment_method
                if isinstance(payment_method, PaymentMethod)
                else PaymentMethod(str(payment_method).strip().upper())
            )
        except ValueError as exc:
            raise InvalidFinePaymentError(
                f"Unsupported payment method '{payment_method}'."
            ) from exc

        clean_idem = (
            idempotency_key.strip()
            if idempotency_key and idempotency_key.strip()
            else None
        )
        payment_ts = paid_at or utc_now()

        with write_transaction(self.session_factory) as session:
            fine_repo = FineRepository(session)

            if isinstance(fine_identifier, int) or str(fine_identifier).isdigit():
                fine = fine_repo.get_by_id(int(fine_identifier))
            else:
                fine = fine_repo.get_by_fine_number(str(fine_identifier))

            if fine is None:
                raise FineNotFoundError(f"Fine '{fine_identifier}' was not found.")

            if clean_idem is not None:
                existing_payment = fine_repo.get_payment_by_idempotency_key(clean_idem)
                if existing_payment is not None:
                    if (
                        existing_payment.fine_id == fine.id
                        and quantize_money(existing_payment.amount) == payment_amount
                    ):
                        return existing_payment
                    raise InvalidFinePaymentError(
                        f"Idempotency key '{clean_idem}' was already used for a different payment."
                    )

            fine.apply_payment(payment_amount)

            local_day = to_local_date(payment_ts, self.library_timezone)
            payment = FinePayment(
                receipt_number=fine_repo.next_receipt_number(local_day),
                fine_id=fine.id,
                member_id=fine.member_id,
                received_by_user_id=actor_user_id,
                branch_id=branch_id or fine.loan.issuing_branch_id,
                amount=payment_amount,
                payment_method=method_enum.value,
                idempotency_key=clean_idem,
                reference_note=reference_note.strip()
                if reference_note and reference_note.strip()
                else None,
                paid_at=payment_ts,
            )
            fine_repo.add_payment(payment)

            AuditRepository(session).record(
                event_type=AuditEventType.FINE_PAYMENT_RECORDED,
                entity_type="FINE_PAYMENT",
                entity_id=payment.receipt_number,
                actor_user_id=actor_user_id,
                branch_id=payment.branch_id,
                summary=(
                    f"Recorded {method_enum.value} payment {payment.receipt_number} of "
                    f"INR {payment_amount} on fine {fine.fine_number} "
                    f"(remaining INR {fine.outstanding_balance})."
                ),
                details={
                    "fine_number": fine.fine_number,
                    "amount": str(payment_amount),
                    "remaining_balance": str(fine.outstanding_balance),
                    "fine_status": fine.status,
                },
            )
            return fine_repo.get_payment_by_idempotency_key(clean_idem) if clean_idem else payment

    def waive_fine(
        self,
        fine_id: int,
        *,
        reason: str,
        actor_user_id: int,
    ) -> Fine:
        clean_reason = reason.strip()
        if len(clean_reason) < 5:
            raise InvalidFinePaymentError(
                "Administrative waiver requires a documented reason (at least 5 characters)."
            )
        with write_transaction(self.session_factory) as session:
            fine_repo = FineRepository(session)
            fine = fine_repo.get_by_id(fine_id)
            if fine is None:
                raise FineNotFoundError(f"Fine #{fine_id} was not found.")
            waived = fine.waive_remaining()
            AuditRepository(session).record(
                event_type=AuditEventType.FINE_WAIVED,
                entity_type="FINE",
                entity_id=fine.fine_number,
                actor_user_id=actor_user_id,
                branch_id=fine.loan.issuing_branch_id,
                summary=f"Waived INR {waived} on fine {fine.fine_number}: {clean_reason}",
                details={"waived_amount": str(waived), "reason": clean_reason},
            )
            return fine_repo.get_by_id(fine.id) or fine

    def get_fine(self, fine_id: int) -> Fine:
        with read_session(self.session_factory) as session:
            fine = FineRepository(session).get_by_id(fine_id)
            if fine is None:
                raise FineNotFoundError(f"Fine #{fine_id} was not found.")
            return fine

    def get_member_outstanding_balance(self, member_id: int) -> Decimal:
        with read_session(self.session_factory) as session:
            if MemberRepository(session).get_by_id(member_id) is None:
                raise MemberNotFoundError(f"Member #{member_id} was not found.")
            return FineRepository(session).get_member_outstanding_balance(member_id)

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
        with read_session(self.session_factory) as session:
            return FineRepository(session).search_fines(
                query=query,
                status=status,
                branch_id=branch_id,
                outstanding_only=outstanding_only,
                page=page,
                per_page=per_page,
            )

    def search_payments(
        self,
        *,
        query: str | None = None,
        branch_id: int | None = None,
        payment_method: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> Page[FinePayment]:
        with read_session(self.session_factory) as session:
            return FineRepository(session).search_payments(
                query=query,
                branch_id=branch_id,
                payment_method=payment_method,
                page=page,
                per_page=per_page,
            )

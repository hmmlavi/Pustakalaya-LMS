"""Circulation service coordinating atomic issue, return, lost-copy, and renewal workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.domain.enums import (
    AuditEventType,
    CopyStatus,
    LoanStatus,
    ReservationStatus,
)
from app.domain.exceptions import (
    BranchNotFoundError,
    CirculationError,
    CopyNotFoundError,
    CopyReservedError,
    CopyUnavailableError,
    LoanAlreadyReturnedError,
    LoanNotFoundError,
    MemberNotFoundError,
    RenewalLimitExceededError,
)
from app.domain.policies import FineBreakdown, PolicyFactory
from app.domain.value_objects import local_today, to_local_date, utc_now
from app.infrastructure.transactions import read_session, write_transaction
from app.models import BookCopy, Fine, Loan, Member, Reservation
from app.repositories import (
    AuditRepository,
    BranchRepository,
    CopyRepository,
    FineRepository,
    HolidayRepository,
    LoanRepository,
    MemberRepository,
    Page,
    ReservationRepository,
)
from app.services.fine_service import FineService
from app.services.reservation_service import ReservationService


@dataclass(frozen=True, slots=True)
class ReturnResult:
    loan: Loan
    fine: Fine | None
    fine_breakdown: FineBreakdown
    allocated_reservation: Reservation | None


class CirculationService:
    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        fine_service: FineService | None = None,
        reservation_service: ReservationService | None = None,
        library_timezone: str = "Asia/Kolkata",
        pickup_hold_days: int = 3,
    ) -> None:
        self.session_factory = session_factory
        self.library_timezone = library_timezone
        self.fine_service = fine_service or FineService(
            session_factory, library_timezone=library_timezone
        )
        self.reservation_service = reservation_service or ReservationService(
            session_factory,
            library_timezone=library_timezone,
            pickup_hold_days=pickup_hold_days,
        )

    @staticmethod
    def _resolve_member(session, identifier: int | str) -> Member:
        repo = MemberRepository(session)
        if isinstance(identifier, int) or str(identifier).strip().isdigit():
            member = repo.get_by_id(int(str(identifier).strip()))
        else:
            member = repo.get_by_member_code(str(identifier).strip())
        if member is None:
            raise MemberNotFoundError(f"Member '{identifier}' was not found.")
        return member

    @staticmethod
    def _resolve_copy(session, identifier: int | str) -> BookCopy:
        repo = CopyRepository(session)
        if isinstance(identifier, int) or str(identifier).strip().isdigit():
            copy = repo.get_by_id(int(str(identifier).strip()))
        else:
            copy = repo.get_by_accession(str(identifier).strip())
        if copy is None:
            raise CopyNotFoundError(f"Physical copy '{identifier}' was not found.")
        return copy

    def issue_copy(
        self,
        *,
        member_identifier: int | str,
        copy_identifier: int | str,
        issuing_branch_id: int | None = None,
        actor_user_id: int | None = None,
        checkout_at: datetime | None = None,
        _before_commit_hook: Callable[[], None] | None = None,
    ) -> Loan:
        """Issue a physical copy to an eligible member inside an atomic IMMEDIATE transaction."""
        now_ts = checkout_at or utc_now()
        checkout_day = to_local_date(now_ts, self.library_timezone)

        with write_transaction(self.session_factory) as session:
            # 1 & 2. Locate member and physical copy
            member = self._resolve_member(session, member_identifier)
            copy = self._resolve_copy(session, copy_identifier)

            # 10. Validate issuing branch (must be active and match copy's owning branch)
            target_branch_id = (
                issuing_branch_id
                if issuing_branch_id is not None
                else copy.owning_branch_id
            )
            branch = BranchRepository(session).get_by_id(target_branch_id)
            if branch is None or not branch.is_active:
                raise BranchNotFoundError(
                    f"Issuing branch #{target_branch_id} is not active or does not exist."
                )
            if branch.id != copy.owning_branch_id:
                raise CirculationError(
                    f"Copy {copy.accession_number} belongs to {copy.owning_branch.name} ({copy.owning_branch.code}) "
                    f"and cannot be issued from {branch.name} without an inter-branch transfer."
                )

            loan_repo = LoanRepository(session)
            res_repo = ReservationRepository(session)
            copy_repo = CopyRepository(session)
            fine_repo = FineRepository(session)

            # 3, 4, 5, 6. Validate member standing, unpaid fines, overdue loans, and borrowing limit
            active_loans = loan_repo.count_active_for_member(member.id)
            overdue_loans = loan_repo.count_active_overdue_for_member(
                member.id, checkout_day
            )
            unpaid_balance = fine_repo.get_member_outstanding_balance(member.id)
            policy = PolicyFactory.create_borrowing_policy(member.tier)
            policy.assert_can_issue(
                member_status=member.status_enum,
                expiry_date=member.expiry_date,
                active_loans_count=active_loans,
                active_overdue_count=overdue_loans,
                outstanding_fine_balance=unpaid_balance,
                as_of_date=checkout_day,
            )

            # 7. Validate copy state (AVAILABLE or RESERVED; rejects ON_LOAN, LOST, DAMAGED, WITHDRAWN)
            copy.assert_can_issue()

            # 8. Validate reservation allocation rules
            ready_reservation = res_repo.get_ready_for_copy(copy.id)
            fulfilled_reservation: Reservation | None = None

            if copy.status_enum == CopyStatus.RESERVED:
                if ready_reservation is None:
                    raise CopyReservedError(
                        f"Copy {copy.accession_number} is marked RESERVED without a valid pickup allocation."
                    )
                if (
                    ready_reservation.pickup_deadline_at is not None
                    and ready_reservation.pickup_deadline_at < now_ts
                ):
                    raise CopyReservedError(
                        f"Pickup deadline for reservation {ready_reservation.reservation_number} has expired; "
                        "run reservation expiration processing first."
                    )
                if ready_reservation.member_id != member.id:
                    raise CopyReservedError(
                        f"Copy {copy.accession_number} is reserved for member "
                        f"{ready_reservation.member.member_code} ({ready_reservation.reservation_number})."
                    )
                fulfilled_reservation = ready_reservation
            else:
                # Copy is AVAILABLE: verify no other READY_FOR_PICKUP hold claims it
                if ready_reservation is not None and ready_reservation.member_id != member.id:
                    raise CopyReservedError(
                        f"Copy {copy.accession_number} is allocated to reservation {ready_reservation.reservation_number}."
                    )
                # If this member happened to have an active WAITING reservation for this book, fulfill it cleanly
                own_res = res_repo.get_active_for_member_and_book(
                    member.id, copy.book_id
                )
                if own_res is not None:
                    if own_res.status_enum == ReservationStatus.WAITING:
                        own_res.allocate_copy(
                            copy.id,
                            allocated_at=now_ts,
                            pickup_deadline_at=now_ts,
                        )
                    fulfilled_reservation = own_res

            # 9. Verify no conflicting active loan exists
            if loan_repo.get_active_loan_for_copy(copy.id) is not None:
                raise CopyUnavailableError(
                    f"Copy {copy.accession_number} already has an active loan."
                )

            # Conditional atomic status transition on book_copies
            expected_status = copy.status_enum
            updated = copy_repo.conditional_transition_status(
                copy.id,
                expected_statuses=[expected_status],
                new_status=CopyStatus.ON_LOAN,
            )
            if not updated:
                raise CopyUnavailableError(
                    f"Copy {copy.accession_number} became unavailable during checkout."
                )

            calendar = HolidayRepository(session).build_calendar()
            due_date = policy.calculate_initial_due_date(
                checkout_day, calendar, branch_id=branch.id
            )

            loan = Loan(
                loan_number=loan_repo.next_loan_number(checkout_day),
                member_id=member.id,
                copy_id=copy.id,
                issuing_branch_id=branch.id,
                issued_by_user_id=actor_user_id,
                checked_out_at=now_ts,
                due_date=due_date,
                status=LoanStatus.ACTIVE.value,
                renewal_count=0,
            )
            try:
                loan_repo.add(loan)
            except IntegrityError as exc:
                raise CopyUnavailableError(
                    f"Copy {copy.accession_number} already has an active loan."
                ) from exc

            audit_repo = AuditRepository(session)
            if fulfilled_reservation is not None:
                fulfilled_reservation.mark_fulfilled(now_ts)
                audit_repo.record(
                    event_type=AuditEventType.RESERVATION_FULFILLED,
                    entity_type="RESERVATION",
                    entity_id=fulfilled_reservation.reservation_number,
                    actor_user_id=actor_user_id,
                    branch_id=branch.id,
                    summary=(
                        f"Fulfilled reservation {fulfilled_reservation.reservation_number} "
                        f"via loan {loan.loan_number}."
                    ),
                )

            audit_repo.record(
                event_type=AuditEventType.LOAN_ISSUED,
                entity_type="LOAN",
                entity_id=loan.loan_number,
                actor_user_id=actor_user_id,
                branch_id=branch.id,
                summary=(
                    f"Issued copy {copy.accession_number} ('{copy.book.title}') "
                    f"to member {member.member_code}; due {due_date.isoformat()}."
                ),
                details={
                    "member_code": member.member_code,
                    "copy_accession": copy.accession_number,
                    "due_date": due_date.isoformat(),
                    "branch_code": branch.code,
                },
            )

            if _before_commit_hook is not None:
                _before_commit_hook()

            return loan_repo.get_by_id(loan.id) or loan

    def return_copy(
        self,
        *,
        loan_id: int | None = None,
        loan_number: str | None = None,
        copy_identifier: int | str | None = None,
        returned_branch_id: int | None = None,
        returned_damaged: bool = False,
        return_notes: str | None = None,
        actor_user_id: int | None = None,
        returned_at: datetime | None = None,
        _before_commit_hook: Callable[[], None] | None = None,
    ) -> ReturnResult:
        """Process a book return atomically, assessing fines and allocating waiting reservations."""
        now_ts = returned_at or utc_now()
        return_local_date = to_local_date(now_ts, self.library_timezone)

        with write_transaction(self.session_factory) as session:
            loan_repo = LoanRepository(session)
            loan: Loan | None = None

            if loan_id is not None:
                loan = loan_repo.get_by_id(loan_id)
                if loan is None:
                    raise LoanNotFoundError(f"Loan #{loan_id} was not found.")
                if loan.status_enum != LoanStatus.ACTIVE:
                    raise LoanAlreadyReturnedError(
                        f"Loan {loan.loan_number} was already closed with status {loan.status}."
                    )
            elif loan_number and loan_number.strip():
                loan = loan_repo.get_by_loan_number(loan_number.strip())
                if loan is None:
                    raise LoanNotFoundError(
                        f"Loan '{loan_number.strip()}' was not found."
                    )
                if loan.status_enum != LoanStatus.ACTIVE:
                    raise LoanAlreadyReturnedError(
                        f"Loan {loan.loan_number} was already closed with status {loan.status}."
                    )
            elif copy_identifier is not None and str(copy_identifier).strip():
                copy_obj = self._resolve_copy(session, copy_identifier)
                loan = loan_repo.get_active_loan_for_copy(copy_obj.id)
                if loan is None:
                    if copy_obj.loans:
                        raise LoanAlreadyReturnedError(
                            f"Copy {copy_obj.accession_number} has no active loan (already returned)."
                        )
                    raise LoanNotFoundError(
                        f"No active loan exists for copy {copy_obj.accession_number}."
                    )
            else:
                raise LoanNotFoundError(
                    "Provide a loan number, loan ID, or copy accession number to process a return."
                )

            ret_branch_id = returned_branch_id or loan.issuing_branch_id
            if BranchRepository(session).get_by_id(ret_branch_id) is None:
                raise BranchNotFoundError(
                    f"Return branch #{ret_branch_id} was not found."
                )

            # Validate return timestamp is not prior to checkout date
            checkout_local_date = to_local_date(
                loan.checked_out_at, self.library_timezone
            )
            if return_local_date < checkout_local_date:
                raise CirculationError(
                    f"Return date ({return_local_date}) cannot precede checkout date ({checkout_local_date})."
                )

            # Mark loan returned
            loan.mark_returned(
                now_ts,
                returned_branch_id=ret_branch_id,
                notes=return_notes.strip()
                if return_notes and return_notes.strip()
                else None,
            )

            # Calculate and assess fine if applicable
            breakdown = self.fine_service.calculate_loan_fine_in_session(
                session, loan, effective_return_date=return_local_date
            )
            fine = self.fine_service.assess_overdue_fine_in_session(
                session,
                loan,
                breakdown,
                assessed_at=now_ts,
                actor_user_id=actor_user_id,
                branch_id=ret_branch_id,
            )

            copy = loan.copy
            audit_repo = AuditRepository(session)
            allocated_reservation: Reservation | None = None

            if returned_damaged:
                damage_note = (
                    f"[RETURNED DAMAGED {return_local_date.isoformat()}] "
                    f"{return_notes.strip() if return_notes and return_notes.strip() else 'Reported damaged at return desk'}"
                )
                copy.transition_to(CopyStatus.DAMAGED, note=damage_note)
                audit_repo.record(
                    event_type=AuditEventType.COPY_STATUS_CHANGED,
                    entity_type="BOOK_COPY",
                    entity_id=copy.accession_number,
                    actor_user_id=actor_user_id,
                    branch_id=ret_branch_id,
                    summary=f"Copy {copy.accession_number} marked DAMAGED upon return of loan {loan.loan_number}.",
                )
            else:
                allocated_reservation = (
                    self.reservation_service.allocate_copy_to_next_waiting_in_session(
                        session,
                        copy,
                        now_utc=now_ts,
                        actor_user_id=actor_user_id,
                    )
                )

            audit_repo.record(
                event_type=AuditEventType.LOAN_RETURNED,
                entity_type="LOAN",
                entity_id=loan.loan_number,
                actor_user_id=actor_user_id,
                branch_id=ret_branch_id,
                summary=(
                    f"Returned copy {copy.accession_number} from loan {loan.loan_number} "
                    f"(copy_status={copy.status}, fine=INR {breakdown.assessed_amount})."
                ),
                details={
                    "copy_accession": copy.accession_number,
                    "member_code": loan.member.member_code,
                    "assessed_fine": str(breakdown.assessed_amount),
                    "returned_damaged": returned_damaged,
                    "allocated_reservation": allocated_reservation.reservation_number
                    if allocated_reservation
                    else None,
                },
            )

            if _before_commit_hook is not None:
                _before_commit_hook()

            refreshed_loan = loan_repo.get_by_id(loan.id) or loan
            refreshed_fine = (
                FineRepository(session).get_by_id(fine.id)
                if fine is not None
                else None
            )
            refreshed_res = (
                ReservationRepository(session).get_by_id(allocated_reservation.id)
                if allocated_reservation is not None
                else None
            )
            return ReturnResult(
                loan=refreshed_loan,
                fine=refreshed_fine,
                fine_breakdown=breakdown,
                allocated_reservation=refreshed_res,
            )

    def report_loan_copy_lost(
        self,
        loan_id: int,
        *,
        notes: str | None = None,
        actor_user_id: int | None = None,
        recorded_at: datetime | None = None,
    ) -> tuple[Loan, Fine | None]:
        """Explicit workflow for reporting an actively borrowed copy as LOST."""
        now_ts = recorded_at or utc_now()
        eval_date = to_local_date(now_ts, self.library_timezone)

        with write_transaction(self.session_factory) as session:
            loan_repo = LoanRepository(session)
            loan = loan_repo.get_by_id(loan_id)
            if loan is None:
                raise LoanNotFoundError(f"Loan #{loan_id} was not found.")
            if loan.status_enum != LoanStatus.ACTIVE:
                raise LoanAlreadyReturnedError(
                    f"Loan {loan.loan_number} is already closed with status {loan.status}."
                )

            clean_note = (
                notes.strip()
                if notes and notes.strip()
                else "Reported lost by borrower"
            )
            loan.mark_lost(now_ts, notes=clean_note)
            loan.copy.transition_to(
                CopyStatus.LOST,
                note=f"[REPORTED LOST {eval_date.isoformat()}] {clean_note}",
            )

            breakdown = self.fine_service.calculate_loan_fine_in_session(
                session, loan, effective_return_date=eval_date
            )
            fine = self.fine_service.assess_overdue_fine_in_session(
                session,
                loan,
                breakdown,
                assessed_at=now_ts,
                reason="Overdue fine accrued prior to lost-copy report",
                actor_user_id=actor_user_id,
                branch_id=loan.issuing_branch_id,
            )

            audit_repo = AuditRepository(session)
            audit_repo.record(
                event_type=AuditEventType.COPY_STATUS_CHANGED,
                entity_type="BOOK_COPY",
                entity_id=loan.copy.accession_number,
                actor_user_id=actor_user_id,
                branch_id=loan.issuing_branch_id,
                summary=f"Copy {loan.copy.accession_number} marked LOST from loan {loan.loan_number}.",
            )
            audit_repo.record(
                event_type=AuditEventType.LOAN_REPORTED_LOST,
                entity_type="LOAN",
                entity_id=loan.loan_number,
                actor_user_id=actor_user_id,
                branch_id=loan.issuing_branch_id,
                summary=f"Loan {loan.loan_number} closed as LOST ({clean_note}).",
            )
            return loan_repo.get_by_id(loan.id) or loan, fine

    def renew_loan(
        self,
        loan_identifier: int | str,
        *,
        actor_user_id: int | None = None,
        renewed_at: datetime | None = None,
    ) -> Loan:
        """Renew an active loan atomically using tier policies and reservation conflict checks."""
        now_ts = renewed_at or utc_now()
        today = to_local_date(now_ts, self.library_timezone)

        with write_transaction(self.session_factory) as session:
            loan_repo = LoanRepository(session)
            if isinstance(loan_identifier, int) or str(loan_identifier).strip().isdigit():
                loan = loan_repo.get_by_id(int(str(loan_identifier).strip()))
            else:
                loan = loan_repo.get_by_loan_number(str(loan_identifier).strip())

            if loan is None:
                raise LoanNotFoundError(f"Loan '{loan_identifier}' was not found.")
            if loan.status_enum != LoanStatus.ACTIVE or loan.returned_at is not None:
                raise LoanAlreadyReturnedError(
                    f"Loan {loan.loan_number} has already been returned or closed and cannot be renewed."
                )

            member = loan.member
            unpaid_balance = FineRepository(session).get_member_outstanding_balance(
                member.id
            )
            competing_reservation = ReservationRepository(
                session
            ).has_competing_reservation(
                loan.copy.book_id, exclude_member_id=member.id
            )

            policy = PolicyFactory.create_borrowing_policy(member.tier)
            policy.assert_can_renew(
                member_status=member.status_enum,
                expiry_date=member.expiry_date,
                outstanding_fine_balance=unpaid_balance,
                current_renewal_count=loan.renewal_count,
                current_due_date=loan.due_date,
                has_competing_reservation=competing_reservation,
                as_of_date=today,
            )

            calendar = HolidayRepository(session).build_calendar()
            old_due_date = loan.due_date
            expected_count = loan.renewal_count
            new_due_date = policy.calculate_renewal_due_date(
                old_due_date, calendar, branch_id=loan.issuing_branch_id
            )

            applied = loan_repo.conditional_apply_renewal(
                loan.id,
                expected_renewal_count=expected_count,
                max_renewals=member.tier.max_renewals,
                new_due_date=new_due_date,
            )
            if not applied:
                raise RenewalLimitExceededError(
                    f"Loan {loan.loan_number} could not be renewed due to a concurrent state change or renewal limit."
                )

            AuditRepository(session).record(
                event_type=AuditEventType.LOAN_RENEWED,
                entity_type="LOAN",
                entity_id=loan.loan_number,
                actor_user_id=actor_user_id,
                branch_id=loan.issuing_branch_id,
                summary=(
                    f"Renewed loan {loan.loan_number} (#{loan.renewal_count}/{member.tier.max_renewals}); "
                    f"due date extended from {old_due_date.isoformat()} to {new_due_date.isoformat()}."
                ),
                details={
                    "previous_due_date": old_due_date.isoformat(),
                    "new_due_date": new_due_date.isoformat(),
                    "renewal_count": loan.renewal_count,
                },
            )
            return loan_repo.get_by_id(loan.id) or loan

    def get_loan(self, loan_id: int) -> Loan:
        with read_session(self.session_factory) as session:
            loan = LoanRepository(session).get_by_id(loan_id)
            if loan is None:
                raise LoanNotFoundError(f"Loan #{loan_id} was not found.")
            return loan

    def search_loans(
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
        eval_date = as_of_date or local_today(self.library_timezone)
        with read_session(self.session_factory) as session:
            return LoanRepository(session).search(
                query=query,
                status=status,
                branch_id=branch_id,
                overdue_only=overdue_only,
                as_of_date=eval_date,
                member_id=member_id,
                page=page,
                per_page=per_page,
            )

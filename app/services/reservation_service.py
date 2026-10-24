"""Reservation queue management service enforcing deterministic FIFO allocation."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import (
    AuditEventType,
    CopyStatus,
    MembershipStatus,
    ReservationStatus,
)
from app.domain.exceptions import (
    BookNotFoundError,
    BranchNotFoundError,
    EligibilityError,
    MemberNotFoundError,
    ReservationConflictError,
    ReservationNotFoundError,
)
from app.domain.policies import PolicyFactory
from app.domain.value_objects import (
    ZERO_MONEY,
    end_of_local_day_utc,
    quantize_money,
    to_local_date,
    utc_now,
)
from app.infrastructure.transactions import read_session, write_transaction
from app.models import BookCopy, Member, Reservation
from app.repositories import (
    AuditRepository,
    BookRepository,
    BranchRepository,
    CopyRepository,
    FineRepository,
    HolidayRepository,
    LoanRepository,
    MemberRepository,
    Page,
    ReservationRepository,
)


class ReservationService:
    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        library_timezone: str = "Asia/Kolkata",
        pickup_hold_days: int = 3,
    ) -> None:
        self.session_factory = session_factory
        self.library_timezone = library_timezone
        self.pickup_hold_days = max(1, int(pickup_hold_days))

    @staticmethod
    def _is_member_eligible_for_hold_allocation(
        session: Session, member: Member, today
    ) -> bool:
        """Check whether a waiting member is eligible to receive a hold allocation."""
        if not member.tier.borrowing_enabled:
            return False
        if member.status_enum != MembershipStatus.ACTIVE:
            return False
        if member.is_expired(today):
            return False
        balance = FineRepository(session).get_member_outstanding_balance(member.id)
        threshold = quantize_money(member.tier.fine_blocking_threshold)
        if threshold <= ZERO_MONEY and balance > ZERO_MONEY:
            return False
        if threshold > ZERO_MONEY and balance >= threshold:
            return False
        return True

    def allocate_copy_to_next_waiting_in_session(
        self,
        session: Session,
        copy: BookCopy,
        *,
        now_utc: datetime,
        actor_user_id: int | None = None,
    ) -> Reservation | None:
        """Allocate a returned or released copy to the next eligible FIFO waiting reservation.

        If no eligible waiting reservation exists, transitions the copy to AVAILABLE.
        """
        res_repo = ReservationRepository(session)
        # Defensive check: never double-allocate a copy
        already_allocated = res_repo.get_ready_for_copy(copy.id)
        if already_allocated is not None:
            return already_allocated

        today = to_local_date(now_utc, self.library_timezone)
        calendar = HolidayRepository(session).build_calendar()

        waiting_queue = res_repo.list_waiting_fifo(
            copy.book_id, branch_id=copy.owning_branch_id
        )

        for candidate in waiting_queue:
            if not self._is_member_eligible_for_hold_allocation(
                session, candidate.member, today
            ):
                continue

            raw_deadline_day = today + timedelta(days=self.pickup_hold_days)
            deadline_day = calendar.adjust_to_operating_day(
                raw_deadline_day, branch_id=copy.owning_branch_id
            )
            deadline_utc = end_of_local_day_utc(deadline_day, self.library_timezone)

            candidate.allocate_copy(
                copy.id,
                allocated_at=now_utc,
                pickup_deadline_at=deadline_utc,
            )
            copy.transition_to(CopyStatus.RESERVED)
            session.flush()

            AuditRepository(session).record(
                event_type=AuditEventType.RESERVATION_ALLOCATED,
                entity_type="RESERVATION",
                entity_id=candidate.reservation_number,
                actor_user_id=actor_user_id,
                branch_id=copy.owning_branch_id,
                summary=(
                    f"Allocated copy {copy.accession_number} to reservation "
                    f"{candidate.reservation_number} for member {candidate.member.member_code} "
                    f"(pickup by {deadline_day.isoformat()})."
                ),
                details={
                    "copy_accession": copy.accession_number,
                    "member_code": candidate.member.member_code,
                    "pickup_deadline_date": deadline_day.isoformat(),
                },
            )
            return candidate

        # No eligible waiting reservation: make copy freely AVAILABLE
        copy.transition_to(CopyStatus.AVAILABLE)
        session.flush()
        return None

    def create_reservation(
        self,
        *,
        member_identifier: int | str,
        book_id: int,
        preferred_branch_id: int | None = None,
        notes: str | None = None,
        actor_user_id: int | None = None,
        created_at: datetime | None = None,
    ) -> Reservation:
        now_ts = created_at or utc_now()
        today = to_local_date(now_ts, self.library_timezone)

        with write_transaction(self.session_factory) as session:
            member_repo = MemberRepository(session)
            if isinstance(member_identifier, int) or str(member_identifier).isdigit():
                member = member_repo.get_by_id(int(member_identifier))
            else:
                member = member_repo.get_by_member_code(str(member_identifier))
            if member is None:
                raise MemberNotFoundError(
                    f"Member '{member_identifier}' was not found."
                )

            book = BookRepository(session).get_by_id(book_id)
            if book is None:
                raise BookNotFoundError(f"Book #{book_id} was not found.")

            if preferred_branch_id is not None:
                branch = BranchRepository(session).get_by_id(preferred_branch_id)
                if branch is None or not branch.is_active:
                    raise BranchNotFoundError(
                        f"Active branch #{preferred_branch_id} was not found."
                    )

            res_repo = ReservationRepository(session)
            loan_repo = LoanRepository(session)
            copy_repo = CopyRepository(session)
            fine_repo = FineRepository(session)

            if res_repo.get_active_for_member_and_book(member.id, book.id) is not None:
                raise ReservationConflictError(
                    f"Member {member.member_code} already holds an active reservation for '{book.title}'."
                )

            if loan_repo.member_has_active_loan_for_book(member.id, book.id):
                raise ReservationConflictError(
                    f"Member {member.member_code} currently has an active loan for '{book.title}'."
                )

            unpaid_balance = fine_repo.get_member_outstanding_balance(member.id)
            active_res_count = res_repo.count_active_for_member(member.id)
            policy = PolicyFactory.create_borrowing_policy(member.tier)
            policy.assert_can_reserve(
                member_status=member.status_enum,
                expiry_date=member.expiry_date,
                outstanding_fine_balance=unpaid_balance,
                active_reservations_count=active_res_count,
                as_of_date=today,
            )

            circulatable = copy_repo.count_circulatable_for_book(
                book.id, branch_id=preferred_branch_id
            )
            if circulatable <= 0:
                scope_msg = (
                    "at the selected branch"
                    if preferred_branch_id is not None
                    else "in the library network"
                )
                raise ReservationConflictError(
                    f"No circulatable physical copies exist for '{book.title}' {scope_msg}."
                )

            available_now = copy_repo.count_available_for_book(
                book.id, branch_id=preferred_branch_id
            )
            if available_now > 0:
                raise ReservationConflictError(
                    f"There are {available_now} AVAILABLE copy/copies of '{book.title}'; "
                    "please issue an available copy directly instead of placing a reservation."
                )

            reservation = Reservation(
                reservation_number=res_repo.next_reservation_number(today),
                book_id=book.id,
                member_id=member.id,
                preferred_branch_id=preferred_branch_id,
                status=ReservationStatus.WAITING.value,
                notes=notes.strip() if notes and notes.strip() else None,
                created_at=now_ts,
                updated_at=now_ts,
            )
            try:
                res_repo.add(reservation)
            except IntegrityError as exc:
                raise ReservationConflictError(
                    "Duplicate active reservation detected for this member and title."
                ) from exc

            queue_pos = res_repo.get_queue_position(reservation)
            AuditRepository(session).record(
                event_type=AuditEventType.RESERVATION_CREATED,
                entity_type="RESERVATION",
                entity_id=reservation.reservation_number,
                actor_user_id=actor_user_id,
                branch_id=preferred_branch_id,
                summary=(
                    f"Created reservation {reservation.reservation_number} for member "
                    f"{member.member_code} on '{book.title}' (queue position #{queue_pos})."
                ),
                details={
                    "book_id": book.id,
                    "member_code": member.member_code,
                    "queue_position": queue_pos,
                },
            )
            return res_repo.get_by_id(reservation.id) or reservation

    def cancel_reservation(
        self,
        reservation_id: int,
        *,
        reason: str | None = None,
        actor_user_id: int | None = None,
        cancelled_at: datetime | None = None,
    ) -> Reservation:
        now_ts = cancelled_at or utc_now()
        with write_transaction(self.session_factory) as session:
            res_repo = ReservationRepository(session)
            reservation = res_repo.get_by_id(reservation_id)
            if reservation is None:
                raise ReservationNotFoundError(
                    f"Reservation #{reservation_id} was not found."
                )

            was_ready = reservation.status_enum == ReservationStatus.READY_FOR_PICKUP
            held_copy = reservation.allocated_copy if was_ready else None

            reservation.cancel(now_ts, note=reason)
            session.flush()

            AuditRepository(session).record(
                event_type=AuditEventType.RESERVATION_CANCELLED,
                entity_type="RESERVATION",
                entity_id=reservation.reservation_number,
                actor_user_id=actor_user_id,
                branch_id=reservation.preferred_branch_id,
                summary=f"Cancelled reservation {reservation.reservation_number}.",
                details={"reason": reason},
            )

            if was_ready and held_copy is not None and held_copy.status_enum == CopyStatus.RESERVED:
                self.allocate_copy_to_next_waiting_in_session(
                    session,
                    held_copy,
                    now_utc=now_ts,
                    actor_user_id=actor_user_id,
                )

            return res_repo.get_by_id(reservation.id) or reservation

    def process_expired_reservations(
        self,
        *,
        as_of_utc: datetime | None = None,
        actor_user_id: int | None = None,
    ) -> list[Reservation]:
        """Expire all READY_FOR_PICKUP reservations past pickup_deadline_at and reallocate copies.

        Safe and idempotent when executed repeatedly.
        """
        eval_utc = as_of_utc or utc_now()
        processed_ids: list[int] = []

        with write_transaction(self.session_factory) as session:
            res_repo = ReservationRepository(session)
            expired_holds = res_repo.list_expired_ready_holds(eval_utc)
            for hold in expired_holds:
                held_copy = hold.allocated_copy
                hold.expire(eval_utc)
                session.flush()

                AuditRepository(session).record(
                    event_type=AuditEventType.RESERVATION_EXPIRED,
                    entity_type="RESERVATION",
                    entity_id=hold.reservation_number,
                    actor_user_id=actor_user_id,
                    branch_id=held_copy.owning_branch_id if held_copy else hold.preferred_branch_id,
                    summary=(
                        f"Expired unclaimed pickup hold {hold.reservation_number} "
                        f"(deadline {hold.pickup_deadline_at.isoformat() if hold.pickup_deadline_at else 'N/A'})."
                    ),
                )

                if held_copy is not None and held_copy.status_enum == CopyStatus.RESERVED:
                    self.allocate_copy_to_next_waiting_in_session(
                        session,
                        held_copy,
                        now_utc=eval_utc,
                        actor_user_id=actor_user_id,
                    )
                processed_ids.append(hold.id)

            return [
                res_repo.get_by_id(rid)
                for rid in processed_ids
                if res_repo.get_by_id(rid) is not None
            ]  # type: ignore[misc]

    def get_reservation(self, reservation_id: int) -> Reservation:
        with read_session(self.session_factory) as session:
            res = ReservationRepository(session).get_by_id(reservation_id)
            if res is None:
                raise ReservationNotFoundError(
                    f"Reservation #{reservation_id} was not found."
                )
            return res

    def get_queue_position(self, reservation_id: int) -> int | None:
        with read_session(self.session_factory) as session:
            res_repo = ReservationRepository(session)
            res = res_repo.get_by_id(reservation_id)
            if res is None:
                raise ReservationNotFoundError(
                    f"Reservation #{reservation_id} was not found."
                )
            return res_repo.get_queue_position(res)

    def get_waiting_queue_for_book(
        self, book_id: int, *, branch_id: int | None = None
    ) -> Sequence[Reservation]:
        with read_session(self.session_factory) as session:
            return ReservationRepository(session).list_waiting_fifo(
                book_id, branch_id=branch_id
            )

    def search_reservations(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        branch_id: int | None = None,
        book_id: int | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> tuple[Page[Reservation], dict[int, int | None]]:
        with read_session(self.session_factory) as session:
            res_repo = ReservationRepository(session)
            page_result = res_repo.search(
                query=query,
                status=status,
                branch_id=branch_id,
                book_id=book_id,
                page=page,
                per_page=per_page,
            )
            positions = {
                r.id: res_repo.get_queue_position(r) for r in page_result.items
            }
            return page_result, positions

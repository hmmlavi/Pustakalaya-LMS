"""Repository for FIFO Reservation queues, holds, and expiration lookups."""

from __future__ import annotations

from datetime import date, datetime
from typing import Sequence
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.domain.enums import ReservationStatus
from app.models import Book, BookCopy, Member, Reservation
from app.repositories.base import Page, escape_like, paginate_statement


class ReservationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _eager_options():
        return (
            joinedload(Reservation.book).selectinload(Book.authors),
            joinedload(Reservation.member).joinedload(Member.tier),
            joinedload(Reservation.preferred_branch),
            joinedload(Reservation.allocated_copy).joinedload(BookCopy.owning_branch),
        )

    def get_by_id(self, reservation_id: int) -> Reservation | None:
        stmt = (
            select(Reservation)
            .options(*self._eager_options())
            .where(Reservation.id == reservation_id)
        )
        return self.session.scalar(stmt)

    def get_by_number(self, reservation_number: str) -> Reservation | None:
        stmt = (
            select(Reservation)
            .options(*self._eager_options())
            .where(
                func.upper(Reservation.reservation_number)
                == reservation_number.strip().upper()
            )
        )
        return self.session.scalar(stmt)

    def get_by_source_reference(self, source_ref: str) -> Reservation | None:
        return self.session.scalar(
            select(Reservation).where(Reservation.source_reference == source_ref)
        )

    def get_active_for_member_and_book(
        self, member_id: int, book_id: int
    ) -> Reservation | None:
        stmt = (
            select(Reservation)
            .options(*self._eager_options())
            .where(
                Reservation.member_id == member_id,
                Reservation.book_id == book_id,
                Reservation.status.in_(
                    [
                        ReservationStatus.WAITING.value,
                        ReservationStatus.READY_FOR_PICKUP.value,
                    ]
                ),
            )
        )
        return self.session.scalar(stmt)

    def get_ready_for_copy(self, copy_id: int) -> Reservation | None:
        stmt = (
            select(Reservation)
            .options(*self._eager_options())
            .where(
                Reservation.allocated_copy_id == copy_id,
                Reservation.status == ReservationStatus.READY_FOR_PICKUP.value,
            )
        )
        return self.session.scalar(stmt)

    def count_active_for_member(self, member_id: int) -> int:
        stmt = select(func.count(Reservation.id)).where(
            Reservation.member_id == member_id,
            Reservation.status.in_(
                [
                    ReservationStatus.WAITING.value,
                    ReservationStatus.READY_FOR_PICKUP.value,
                ]
            ),
        )
        return int(self.session.scalar(stmt) or 0)

    def has_competing_reservation(
        self, book_id: int, *, exclude_member_id: int | None = None
    ) -> bool:
        stmt = select(func.count(Reservation.id)).where(
            Reservation.book_id == book_id,
            Reservation.status.in_(
                [
                    ReservationStatus.WAITING.value,
                    ReservationStatus.READY_FOR_PICKUP.value,
                ]
            ),
        )
        if exclude_member_id is not None:
            stmt = stmt.where(Reservation.member_id != exclude_member_id)
        return int(self.session.scalar(stmt) or 0) > 0

    def list_waiting_fifo(
        self, book_id: int, *, branch_id: int | None = None
    ) -> Sequence[Reservation]:
        """Return WAITING reservations in deterministic FIFO order: (created_at ASC, id ASC)."""
        stmt = (
            select(Reservation)
            .options(*self._eager_options())
            .where(
                Reservation.book_id == book_id,
                Reservation.status == ReservationStatus.WAITING.value,
            )
        )
        if branch_id is not None:
            stmt = stmt.where(
                or_(
                    Reservation.preferred_branch_id.is_(None),
                    Reservation.preferred_branch_id == branch_id,
                )
            )
        stmt = stmt.order_by(Reservation.created_at.asc(), Reservation.id.asc())
        return list(self.session.scalars(stmt).unique().all())

    def get_queue_position(self, reservation: Reservation) -> int | None:
        """Compute 1-based FIFO queue position among WAITING reservations for the same title."""
        if reservation.status != ReservationStatus.WAITING.value:
            return None
        stmt = select(Reservation.id).where(
            Reservation.book_id == reservation.book_id,
            Reservation.status == ReservationStatus.WAITING.value,
        )
        if reservation.preferred_branch_id is not None:
            stmt = stmt.where(
                or_(
                    Reservation.preferred_branch_id.is_(None),
                    Reservation.preferred_branch_id == reservation.preferred_branch_id,
                )
            )
        stmt = stmt.order_by(Reservation.created_at.asc(), Reservation.id.asc())
        ordered_ids = list(self.session.scalars(stmt).all())
        try:
            return ordered_ids.index(reservation.id) + 1
        except ValueError:
            return None

    def list_expired_ready_holds(self, as_of_utc: datetime) -> Sequence[Reservation]:
        stmt = (
            select(Reservation)
            .options(*self._eager_options())
            .where(
                Reservation.status == ReservationStatus.READY_FOR_PICKUP.value,
                Reservation.pickup_deadline_at.is_not(None),
                Reservation.pickup_deadline_at < as_of_utc,
            )
            .order_by(Reservation.pickup_deadline_at.asc(), Reservation.id.asc())
        )
        return list(self.session.scalars(stmt).unique().all())

    def list_for_member(self, member_id: int, *, limit: int = 50) -> Sequence[Reservation]:
        stmt = (
            select(Reservation)
            .options(*self._eager_options())
            .where(Reservation.member_id == member_id)
            .order_by(Reservation.created_at.desc(), Reservation.id.desc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt).unique().all())

    def next_reservation_number(self, ref_date: date) -> str:
        token = uuid.uuid4().hex[:8].upper()
        return f"RSV-{ref_date.strftime('%Y%m%d')}-{token}"

    def search(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        branch_id: int | None = None,
        book_id: int | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> Page[Reservation]:
        stmt = (
            select(Reservation)
            .join(Book, Book.id == Reservation.book_id)
            .join(Member, Member.id == Reservation.member_id)
            .options(*self._eager_options())
        )
        if query and query.strip():
            like_q = f"%{escape_like(query.strip().lower())}%"
            stmt = stmt.where(
                or_(
                    func.lower(Reservation.reservation_number).like(
                        like_q, escape="\\"
                    ),
                    func.lower(Book.title).like(like_q, escape="\\"),
                    func.lower(Member.member_code).like(like_q, escape="\\"),
                    func.lower(Member.full_name).like(like_q, escape="\\"),
                )
            )
        if status and status.strip():
            stmt = stmt.where(Reservation.status == status.strip().upper())
        if branch_id is not None:
            stmt = stmt.where(
                or_(
                    Reservation.preferred_branch_id == branch_id,
                    Reservation.allocated_copy.has(
                        BookCopy.owning_branch_id == branch_id
                    ),
                )
            )
        if book_id is not None:
            stmt = stmt.where(Reservation.book_id == book_id)

        stmt = stmt.order_by(
            Reservation.created_at.asc()
            if status == ReservationStatus.WAITING.value
            else Reservation.created_at.desc(),
            Reservation.id.asc(),
        )
        return paginate_statement(self.session, stmt, page=page, per_page=per_page)

    def add(self, reservation: Reservation) -> Reservation:
        self.session.add(reservation)
        self.session.flush()
        return reservation

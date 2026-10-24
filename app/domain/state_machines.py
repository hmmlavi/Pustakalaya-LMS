"""Explicit state machines governing BookCopy and Reservation lifecycles."""

from __future__ import annotations

from app.domain.enums import CopyStatus, ReservationStatus
from app.domain.exceptions import (
    CopyUnavailableError,
    InvalidStateTransitionError,
    ReservationConflictError,
)


class CopyStateMachine:
    """Authoritative transition rules for physical library copies."""

    # Manual / inventory transitions allowed outside active loan checkout/return
    MANUAL_TRANSITIONS: dict[CopyStatus, frozenset[CopyStatus]] = {
        CopyStatus.AVAILABLE: frozenset(
            {CopyStatus.DAMAGED, CopyStatus.LOST, CopyStatus.WITHDRAWN}
        ),
        CopyStatus.ON_LOAN: frozenset(),  # Active loans must use return / report-lost workflows
        CopyStatus.RESERVED: frozenset(
            {CopyStatus.DAMAGED, CopyStatus.LOST, CopyStatus.WITHDRAWN}
        ),
        CopyStatus.DAMAGED: frozenset({CopyStatus.AVAILABLE, CopyStatus.WITHDRAWN}),
        CopyStatus.LOST: frozenset({CopyStatus.AVAILABLE, CopyStatus.WITHDRAWN}),
        CopyStatus.WITHDRAWN: frozenset(),  # Terminal except via explicit admin correction
    }

    CIRCULATION_TRANSITIONS: dict[CopyStatus, frozenset[CopyStatus]] = {
        CopyStatus.AVAILABLE: frozenset(
            {
                CopyStatus.ON_LOAN,
                CopyStatus.RESERVED,
                CopyStatus.DAMAGED,
                CopyStatus.LOST,
                CopyStatus.WITHDRAWN,
            }
        ),
        CopyStatus.ON_LOAN: frozenset(
            {
                CopyStatus.AVAILABLE,
                CopyStatus.RESERVED,
                CopyStatus.DAMAGED,
                CopyStatus.LOST,
            }
        ),
        CopyStatus.RESERVED: frozenset(
            {
                CopyStatus.ON_LOAN,
                CopyStatus.AVAILABLE,
                CopyStatus.RESERVED,
                CopyStatus.DAMAGED,
                CopyStatus.LOST,
                CopyStatus.WITHDRAWN,
            }
        ),
        CopyStatus.DAMAGED: frozenset({CopyStatus.AVAILABLE, CopyStatus.WITHDRAWN}),
        CopyStatus.LOST: frozenset({CopyStatus.AVAILABLE, CopyStatus.WITHDRAWN}),
        CopyStatus.WITHDRAWN: frozenset({CopyStatus.AVAILABLE}),
    }

    @classmethod
    def assert_can_issue(cls, status: CopyStatus) -> None:
        if status not in (CopyStatus.AVAILABLE, CopyStatus.RESERVED):
            raise CopyUnavailableError(
                f"Copy cannot be issued because its current status is {status.value}."
            )

    @classmethod
    def validate_transition(
        cls,
        current: CopyStatus,
        target: CopyStatus,
        *,
        manual: bool = False,
        admin_override: bool = False,
    ) -> None:
        if current == target and current != CopyStatus.RESERVED:
            return
        if manual:
            if current == CopyStatus.ON_LOAN:
                raise InvalidStateTransitionError(
                    "Cannot manually change status of a copy that is currently ON_LOAN; process a return or lost report first."
                )
            if current == CopyStatus.WITHDRAWN and target == CopyStatus.AVAILABLE and admin_override:
                return
            allowed = cls.MANUAL_TRANSITIONS.get(current, frozenset())
            if target not in allowed:
                raise InvalidStateTransitionError(
                    f"Manual copy status transition from {current.value} to {target.value} is not permitted."
                )
            return

        allowed = cls.CIRCULATION_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise InvalidStateTransitionError(
                f"Copy status transition from {current.value} to {target.value} is not permitted."
            )


class ReservationStateMachine:
    """Authoritative transition rules for reservation lifecycle."""

    ALLOWED_TRANSITIONS: dict[ReservationStatus, frozenset[ReservationStatus]] = {
        ReservationStatus.WAITING: frozenset(
            {
                ReservationStatus.READY_FOR_PICKUP,
                ReservationStatus.CANCELLED,
                ReservationStatus.EXPIRED,
            }
        ),
        ReservationStatus.READY_FOR_PICKUP: frozenset(
            {
                ReservationStatus.FULFILLED,
                ReservationStatus.CANCELLED,
                ReservationStatus.EXPIRED,
            }
        ),
        ReservationStatus.FULFILLED: frozenset(),
        ReservationStatus.CANCELLED: frozenset(),
        ReservationStatus.EXPIRED: frozenset(),
    }

    @classmethod
    def validate_transition(
        cls, current: ReservationStatus, target: ReservationStatus
    ) -> None:
        allowed = cls.ALLOWED_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise ReservationConflictError(
                f"Cannot transition reservation from {current.value} to {target.value}."
            )

"""Unit tests for ISBN validation, monetary precision, state machines, and policies."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.enums import CopyStatus, MembershipStatus, ReservationStatus
from app.domain.exceptions import (
    BorrowingLimitExceededError,
    CopyUnavailableError,
    InvalidISBNError,
    InvalidStateTransitionError,
    MemberNotEligibleError,
    OutstandingFineError,
    RenewalLimitExceededError,
    RenewalNotAllowedError,
    ReservationConflictError,
)
from app.domain.policies import (
    HolidayAwareFinePolicy,
    HolidayCalendar,
    HolidayEntry,
    TierBorrowingPolicy,
    TierPolicyConfig,
)
from app.domain.state_machines import CopyStateMachine, ReservationStateMachine
from app.domain.value_objects import (
    ISBN,
    normalize_email,
    normalize_phone,
    quantize_money,
)


def _sample_tier(code: str = "BASIC") -> TierPolicyConfig:
    if code == "BASIC":
        return TierPolicyConfig(
            code="BASIC",
            name="Basic Citizen",
            max_active_loans=2,
            loan_duration_days=14,
            max_renewals=1,
            renewal_duration_days=7,
            max_active_reservations=2,
            grace_period_days=1,
            daily_fine_rate=Decimal("5.00"),
            max_fine_per_loan=Decimal("150.00"),
            fine_blocking_threshold=Decimal("50.00"),
            borrowing_enabled=True,
        )
    return TierPolicyConfig(
        code="STANDARD",
        name="Standard Reader",
        max_active_loans=5,
        loan_duration_days=21,
        max_renewals=2,
        renewal_duration_days=14,
        max_active_reservations=4,
        grace_period_days=2,
        daily_fine_rate=Decimal("3.00"),
        max_fine_per_loan=Decimal("20.00"),
        fine_blocking_threshold=Decimal("100.00"),
        borrowing_enabled=True,
    )


def test_valid_isbn13_and_hyphen_normalization():
    body12 = "978812671234"
    chk = ISBN.compute_isbn13_check_digit(body12)
    parsed = ISBN.parse(f"978-81-267-1234-{chk}")
    assert parsed is not None
    assert parsed.normalized == body12 + chk


def test_invalid_isbn13_checksum_rejected():
    body12 = "978812671234"
    chk = ISBN.compute_isbn13_check_digit(body12)
    wrong = "0" if chk != "0" else "1"
    with pytest.raises(InvalidISBNError):
        ISBN.parse(body12 + wrong)


def test_valid_isbn10_including_x_check_digit():
    parsed_x = ISBN.parse("0-8044-2957-X")
    assert parsed_x is not None
    assert parsed_x.normalized == "080442957X"

    body9 = "817223498"
    chk = ISBN.compute_isbn10_check_digit(body9)
    parsed = ISBN.parse(f"81-7223-498-{chk}")
    assert parsed is not None
    assert parsed.normalized == body9 + chk


def test_invalid_isbn10_and_malformed_lengths_rejected():
    with pytest.raises(InvalidISBNError):
        ISBN.parse("0-8044-2957-4")
    with pytest.raises(InvalidISBNError):
        ISBN.parse("97812345")
    with pytest.raises(InvalidISBNError):
        ISBN.parse("ABCDEFGHIJ")


def test_optional_missing_isbn_returns_none():
    assert ISBN.parse(None) is None
    assert ISBN.parse("   ") is None


def test_decimal_money_quantization_and_float_rejection():
    assert quantize_money("12.345") == Decimal("12.35")
    assert quantize_money(Decimal("40")) == Decimal("40.00")
    with pytest.raises(TypeError, match="binary float"):
        quantize_money(12.34)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        quantize_money("not-a-number")


def test_email_and_phone_normalization():
    assert normalize_email("  Reader@Pustakalaya.ORG ") == "reader@pustakalaya.org"
    assert normalize_email("   ") is None
    with pytest.raises(ValueError):
        normalize_email("invalid-email")

    assert normalize_phone("+91 (522) 262-0101") == "+915222620101"
    with pytest.raises(ValueError):
        normalize_phone("123")


def test_copy_state_machine_circulation_and_manual_rules():
    CopyStateMachine.assert_can_issue(CopyStatus.AVAILABLE)
    CopyStateMachine.assert_can_issue(CopyStatus.RESERVED)
    with pytest.raises(CopyUnavailableError):
        CopyStateMachine.assert_can_issue(CopyStatus.DAMAGED)
    with pytest.raises(CopyUnavailableError):
        CopyStateMachine.assert_can_issue(CopyStatus.WITHDRAWN)

    # Cannot manually alter an ON_LOAN copy without returning/reporting lost
    with pytest.raises(InvalidStateTransitionError):
        CopyStateMachine.validate_transition(
            CopyStatus.ON_LOAN, CopyStatus.AVAILABLE, manual=True
        )

    # Withdrawn copy requires admin_override to reinstate manually
    with pytest.raises(InvalidStateTransitionError):
        CopyStateMachine.validate_transition(
            CopyStatus.WITHDRAWN, CopyStatus.AVAILABLE, manual=True, admin_override=False
        )
    CopyStateMachine.validate_transition(
        CopyStatus.WITHDRAWN, CopyStatus.AVAILABLE, manual=True, admin_override=True
    )


def test_reservation_state_machine_transitions():
    ReservationStateMachine.validate_transition(
        ReservationStatus.WAITING, ReservationStatus.READY_FOR_PICKUP
    )
    ReservationStateMachine.validate_transition(
        ReservationStatus.READY_FOR_PICKUP, ReservationStatus.FULFILLED
    )
    with pytest.raises(ReservationConflictError):
        ReservationStateMachine.validate_transition(
            ReservationStatus.FULFILLED, ReservationStatus.WAITING
        )
    with pytest.raises(ReservationConflictError):
        ReservationStateMachine.validate_transition(
            ReservationStatus.CANCELLED, ReservationStatus.READY_FOR_PICKUP
        )


def test_holiday_calendar_adjusts_network_and_branch_closures():
    cal = HolidayCalendar(
        [
            HolidayEntry(holiday_date=date(2026, 10, 26), name="Network Holiday", branch_id=None),
            HolidayEntry(holiday_date=date(2026, 10, 27), name="Branch 1 Maintenance", branch_id=1),
        ]
    )
    assert cal.adjust_to_operating_day(date(2026, 10, 26), branch_id=1) == date(2026, 10, 28)
    assert cal.adjust_to_operating_day(date(2026, 10, 26), branch_id=2) == date(2026, 10, 27)


def test_holiday_aware_fine_policy_grace_holidays_and_cap():
    cal = HolidayCalendar(
        [HolidayEntry(holiday_date=date(2026, 10, 12), name="Dussehra", branch_id=None)]
    )
    policy = HolidayAwareFinePolicy(_sample_tier("STANDARD"), cal)

    # Returned within 2 operating grace days (Oct 11 open, Oct 12 holiday, Oct 13 open) -> ₹0.00
    within_grace = policy.calculate_fine(
        due_date=date(2026, 10, 10),
        effective_return_date=date(2026, 10, 13),
        branch_id=1,
    )
    assert within_grace.assessed_amount == Decimal("0.00")
    assert within_grace.holiday_days_excluded == 1

    # Returned Oct 14 -> 3 chargeable operating days - 2 grace days = 1 eligible day * ₹3.00 = ₹3.00
    after_grace = policy.calculate_fine(
        due_date=date(2026, 10, 10),
        effective_return_date=date(2026, 10, 14),
        branch_id=1,
    )
    assert after_grace.chargeable_days_after_due == 3
    assert after_grace.eligible_overdue_days == 1
    assert after_grace.assessed_amount == Decimal("3.00")

    # Capped at ₹20.00 max_fine_per_loan
    capped = policy.calculate_fine(
        due_date=date(2026, 9, 1),
        effective_return_date=date(2026, 10, 24),
        branch_id=1,
    )
    assert capped.assessed_amount == Decimal("20.00")
    assert capped.uncapped_amount > Decimal("20.00")


def test_tier_borrowing_policy_blocks_limit_fines_overdue_and_expired():
    policy = TierBorrowingPolicy(_sample_tier("BASIC"))
    today = date(2026, 10, 24)

    # Loan limit reached (2/2)
    with pytest.raises(BorrowingLimitExceededError):
        policy.assert_can_issue(
            member_status=MembershipStatus.ACTIVE,
            expiry_date=date(2027, 5, 1),
            active_loans_count=2,
            active_overdue_count=0,
            outstanding_fine_balance=Decimal("0.00"),
            as_of_date=today,
        )

    # Fine threshold reached (₹50.00 >= ₹50.00)
    with pytest.raises(OutstandingFineError):
        policy.assert_can_issue(
            member_status=MembershipStatus.ACTIVE,
            expiry_date=date(2027, 5, 1),
            active_loans_count=0,
            active_overdue_count=0,
            outstanding_fine_balance=Decimal("50.00"),
            as_of_date=today,
        )

    # Active overdue loan blocks checkout
    with pytest.raises(MemberNotEligibleError):
        policy.assert_can_issue(
            member_status=MembershipStatus.ACTIVE,
            expiry_date=date(2027, 5, 1),
            active_loans_count=1,
            active_overdue_count=1,
            outstanding_fine_balance=Decimal("0.00"),
            as_of_date=today,
        )

    # Expired membership blocked
    with pytest.raises(MemberNotEligibleError):
        policy.assert_can_issue(
            member_status=MembershipStatus.ACTIVE,
            expiry_date=date(2026, 10, 23),
            active_loans_count=0,
            active_overdue_count=0,
            outstanding_fine_balance=Decimal("0.00"),
            as_of_date=today,
        )


def test_tier_borrowing_policy_renewal_restrictions():
    policy = TierBorrowingPolicy(_sample_tier("BASIC"))
    today = date(2026, 10, 24)

    # Overdue loan cannot be renewed
    with pytest.raises(RenewalNotAllowedError):
        policy.assert_can_renew(
            member_status=MembershipStatus.ACTIVE,
            expiry_date=date(2027, 5, 1),
            outstanding_fine_balance=Decimal("0.00"),
            current_renewal_count=0,
            current_due_date=date(2026, 10, 20),
            has_competing_reservation=False,
            as_of_date=today,
        )

    # Max renewals reached
    with pytest.raises(RenewalLimitExceededError):
        policy.assert_can_renew(
            member_status=MembershipStatus.ACTIVE,
            expiry_date=date(2027, 5, 1),
            outstanding_fine_balance=Decimal("0.00"),
            current_renewal_count=1,
            current_due_date=date(2026, 10, 28),
            has_competing_reservation=False,
            as_of_date=today,
        )

    # Competing reservation blocks renewal
    with pytest.raises(ReservationConflictError):
        policy.assert_can_renew(
            member_status=MembershipStatus.ACTIVE,
            expiry_date=date(2027, 5, 1),
            outstanding_fine_balance=Decimal("0.00"),
            current_renewal_count=0,
            current_due_date=date(2026, 10, 28),
            has_competing_reservation=True,
            as_of_date=today,
        )

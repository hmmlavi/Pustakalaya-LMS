"""Additional tests for authentication, password hashing, admin corrections, and reporting."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.enums import CopyStatus, ReservationStatus, UserRole
from app.domain.exceptions import (
    AuthenticationError,
    AuthorizationError,
)


def test_password_hashing_authentication_and_inactive_user_rejection(db_env):
    auth = db_env["auth"]
    user = auth.authenticate("librarian", "LibrarianPassword!99")
    assert user.username == "librarian"
    assert user.role == UserRole.LIBRARIAN.value

    # Wrong password rejected
    with pytest.raises(AuthenticationError):
        auth.authenticate("librarian", "WrongPassword123!")

    # Unknown username rejected without leaking existence
    with pytest.raises(AuthenticationError):
        auth.authenticate("nobody_here", "WrongPassword123!")

    # Password change requires minimum 8 characters
    with pytest.raises(ValueError, match="at least 8 characters"):
        auth.change_own_password(
            user.id,
            current_password="LibrarianPassword!99",
            new_password="short",
        )

    auth.change_own_password(
        user.id,
        current_password="LibrarianPassword!99",
        new_password="UpdatedLibrarianPassword!88",
    )
    assert auth.authenticate("librarian", "UpdatedLibrarianPassword!88").id == user.id

    # Deactivated account cannot log in
    auth.update_staff_user(
        user.id,
        role=UserRole.LIBRARIAN,
        is_active=False,
        branch_id=db_env["branches"][0].id,
        actor_user_id=db_env["admin_user"].id,
    )
    with pytest.raises(AuthenticationError):
        auth.authenticate("librarian", "UpdatedLibrarianPassword!88")


def test_bootstrap_initial_admin_is_idempotent_and_protects_last_admin(db_env):
    existing, created = db_env["auth"].bootstrap_initial_admin(
        username="admin",
        password="AnotherStrongPassword!99",
        full_name="Test Administrator",
    )
    assert created is False
    assert existing.id == db_env["admin_user"].id

    # Cannot deactivate or demote the sole active ADMIN
    with pytest.raises(AuthorizationError):
        db_env["auth"].update_staff_user(
            db_env["admin_user"].id,
            role=UserRole.LIBRARIAN,
            is_active=True,
            actor_user_id=db_env["admin_user"].id,
        )


def test_admin_tier_update_affects_future_loans_without_corrupting_existing(db_env):
    b1, _, _ = db_env["branches"]
    book = db_env["books"].create_book(
        title="Policy Evolution Study",
        isbn=None,
        publisher="Lucknow University Press",
        publication_year=2021,
        language="English",
        category="Public Policy & Governance",
        edition="1st",
        author_names=["Dr. R. K. Tiwari"],
        actor_user_id=db_env["admin_user"].id,
    )
    c1 = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-TIER-01",
        acquisition_date=date(2025, 2, 1),
        actor_user_id=db_env["admin_user"].id,
    )
    c2 = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-TIER-02",
        acquisition_date=date(2025, 2, 1),
        actor_user_id=db_env["admin_user"].id,
    )
    m = db_env["members"].register_member(
        full_name="Tier Policy Reader",
        email="tp@example.org",
        tier_code="BASIC",
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )

    t0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    loan_before = db_env["circulation"].issue_copy(
        member_identifier=m.member_code,
        copy_identifier=c1.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        checkout_at=t0,
    )
    assert loan_before.due_date == date(2026, 10, 15)  # 14 days

    # Admin updates BASIC loan_duration_days from 14 -> 20
    db_env["admin"].update_tier_policy(
        "BASIC",
        max_active_loans=2,
        loan_duration_days=20,
        max_renewals=1,
        renewal_duration_days=7,
        max_active_reservations=2,
        grace_period_days=1,
        daily_fine_rate=Decimal("5.00"),
        max_fine_per_loan=Decimal("150.00"),
        fine_blocking_threshold=Decimal("50.00"),
        borrowing_enabled=True,
        actor_user_id=db_env["admin_user"].id,
    )

    loan_after = db_env["circulation"].issue_copy(
        member_identifier=m.member_code,
        copy_identifier=c2.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        checkout_at=t0,
    )
    assert loan_after.due_date == date(2026, 10, 21)  # 20 days
    refreshed_before = db_env["circulation"].get_loan(loan_before.id)
    assert refreshed_before.due_date == date(2026, 10, 15)


def test_cancelling_ready_for_pickup_reservation_releases_copy_to_available(db_env):
    b1, _, _ = db_env["branches"]
    book = db_env["books"].create_book(
        title="Reservation Cancel Test",
        isbn=None,
        publisher="Test Press",
        publication_year=2020,
        language="English",
        category="World Fiction",
        edition="1st",
        author_names=["Vikram Seth"],
        actor_user_id=db_env["admin_user"].id,
    )
    copy = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-CANCEL-01",
        acquisition_date=date(2025, 3, 1),
        actor_user_id=db_env["admin_user"].id,
    )
    m1 = db_env["members"].register_member(
        full_name="Borrower",
        email="b@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )
    m2 = db_env["members"].register_member(
        full_name="Canceller",
        email="c@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )

    db_env["circulation"].issue_copy(
        member_identifier=m1.member_code,
        copy_identifier=copy.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    res = db_env["reservations"].create_reservation(
        member_identifier=m2.member_code,
        book_id=book.id,
        preferred_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    ret = db_env["circulation"].return_copy(
        copy_identifier=copy.accession_number,
        returned_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    assert ret.loan.copy.status == CopyStatus.RESERVED.value

    cancelled = db_env["reservations"].cancel_reservation(
        res.id,
        reason="Member found another edition",
        actor_user_id=db_env["librarian_user"].id,
    )
    assert cancelled.status == ReservationStatus.CANCELLED.value
    refreshed_copy = db_env["books"].get_copy(copy.id)
    assert refreshed_copy.status == CopyStatus.AVAILABLE.value


def test_dashboard_and_branch_overdue_reporting_accuracy(db_env):
    b1, b2, _ = db_env["branches"]
    book = db_env["books"].create_book(
        title="Overdue Analytics Verification",
        isbn=None,
        publisher="Analytics Press",
        publication_year=2024,
        language="English",
        category="Economics & Development",
        edition="1st",
        author_names=["Amartya Sen"],
        actor_user_id=db_env["admin_user"].id,
    )
    c_hzr = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-RPT-01",
        acquisition_date=date(2025, 1, 1),
        actor_user_id=db_env["admin_user"].id,
    )
    c_gmn = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b2.id,
        accession_number="LKO-GMN-RPT-02",
        acquisition_date=date(2025, 1, 1),
        actor_user_id=db_env["admin_user"].id,
    )
    m = db_env["members"].register_member(
        full_name="Report Reader",
        email="rr@example.org",
        phone="+91-9999988888",
        tier_code="BASIC",
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )
    m2 = db_env["members"].register_member(
        full_name="On-Time Reader",
        email="ontime@example.org",
        tier_code="STANDARD",
        home_branch_id=b2.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )

    # Issue overdue loan at b1 to m, active non-overdue loan at b2 to m2
    db_env["circulation"].issue_copy(
        member_identifier=m.member_code,
        copy_identifier=c_hzr.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        checkout_at=datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc),
    )
    db_env["circulation"].issue_copy(
        member_identifier=m2.member_code,
        copy_identifier=c_gmn.accession_number,
        issuing_branch_id=b2.id,
        actor_user_id=db_env["librarian_user"].id,
        checkout_at=datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc),
    )

    stats = db_env["reports"].get_dashboard_stats(as_of_date=date(2026, 10, 24))
    assert stats.total_titles == 1
    assert stats.total_copies == 2
    assert stats.copies_on_loan == 2
    assert stats.active_loans == 2
    assert stats.overdue_loans == 1

    hzr_overdues = db_env["reports"].get_overdue_report(
        branch_id=b1.id, as_of_date=date(2026, 10, 24)
    )
    gmn_overdues = db_env["reports"].get_overdue_report(
        branch_id=b2.id, as_of_date=date(2026, 10, 24)
    )
    assert hzr_overdues.total == 1
    assert hzr_overdues.items[0].copy_accession == "LKO-HZR-RPT-01"
    assert hzr_overdues.items[0].estimated_fine > Decimal("0.00")
    assert gmn_overdues.total == 0


def test_holiday_registration_and_adjusted_due_date_correction(db_env):
    b1, _, _ = db_env["branches"]
    h = db_env["admin"].create_holiday(
        holiday_date=date(2026, 11, 8),
        name="Special Civic Closure",
        branch_id=None,
        actor_user_id=db_env["admin_user"].id,
    )
    assert any(item.id == h.id for item in db_env["admin"].list_holidays())

    book = db_env["books"].create_book(
        title="Due Date Adjustment Book",
        isbn=None,
        publisher="Test",
        publication_year=2022,
        language="English",
        category="Science & Ecology",
        author_names=["Salim Ali"],
        actor_user_id=db_env["admin_user"].id,
    )
    copy = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-ADJ-01",
        acquisition_date=date(2025, 1, 1),
        actor_user_id=db_env["admin_user"].id,
    )
    m = db_env["members"].register_member(
        full_name="Adjust Reader",
        email="adj@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )
    loan = db_env["circulation"].issue_copy(
        member_identifier=m.member_code,
        copy_identifier=copy.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        checkout_at=datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc),
    )
    updated = db_env["admin"].admin_adjust_loan_due_date(
        loan.id,
        new_due_date=loan.due_date + timedelta(days=5),
        reason="Extended due to severe monsoon disruption",
        actor_user_id=db_env["admin_user"].id,
    )
    assert updated.due_date == loan.due_date + timedelta(days=5)

"""Integration tests for catalog, copies, members, circulation, and FIFO reservations."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.domain.enums import (
    CopyStatus,
    LoanStatus,
    MembershipStatus,
    ReservationStatus,
)
from app.domain.exceptions import (
    BorrowingLimitExceededError,
    CopyReservedError,
    CopyUnavailableError,
    DuplicateRecordError,
    MemberNotEligibleError,
    ReservationConflictError,
)


def _seed_sample_book_and_copies(
    db_env, copy_count: int = 1, isbn: str | None = "9780306406157"
):
    b1, _, _ = db_env["branches"]
    book = db_env["books"].create_book(
        title="Godaan",
        subtitle="A Classic of Hindi Literature",
        isbn=isbn,
        publisher="Lokbharti Prakashan",
        publication_year=1936,
        language="Hindi",
        category="Hindi Literature",
        edition="Centenary Edition",
        description="Novel by Munshi Premchand",
        author_names=["Munshi Premchand"],
        actor_user_id=db_env["admin_user"].id,
    )
    copies = []
    for idx in range(copy_count):
        copies.append(
            db_env["books"].add_copy(
                book_id=book.id,
                owning_branch_id=b1.id,
                accession_number=f"{b1.code}-TEST-{book.id:03d}-{idx + 1:02d}",
                acquisition_date=date(2025, 1, 15),
                actor_user_id=db_env["admin_user"].id,
            )
        )
    return book, copies


def test_catalog_duplicate_isbn_and_missing_isbn_handling(db_env):
    book1, _ = _seed_sample_book_and_copies(db_env, copy_count=1, isbn="9780306406157")
    assert book1.isbn == "9780306406157"

    # Duplicate ISBN rejected even when formatted with hyphens
    with pytest.raises(DuplicateRecordError):
        db_env["books"].create_book(
            title="Another Edition Same ISBN",
            isbn="978-0-306-40615-7",
            publisher="Lokbharti Prakashan",
            publication_year=2001,
            language="Hindi",
            category="Hindi Literature",
            author_names=["Munshi Premchand"],
            actor_user_id=db_env["admin_user"].id,
        )

    # Book without ISBN succeeds cleanly, and exact non-ISBN duplicate is blocked
    unnumbered = db_env["books"].create_book(
        title="Rare Awadhi Pamphlet",
        isbn=None,
        publisher="Naval Kishore Press",
        publication_year=1924,
        language="Awadhi",
        category="Lucknow & Awadh Heritage",
        edition="1st",
        author_names=["Mirza Hadi Ruswa", "Amritlal Nagar"],
        actor_user_id=db_env["admin_user"].id,
    )
    assert unnumbered.isbn is None
    assert len(unnumbered.authors) == 2

    with pytest.raises(DuplicateRecordError):
        db_env["books"].create_book(
            title="rare awadhi pamphlet",
            isbn="",
            publisher="naval kishore press",
            publication_year=1924,
            language="Awadhi",
            category="Lucknow & Awadh Heritage",
            edition="1st",
            author_names=["Mirza Hadi Ruswa"],
            actor_user_id=db_env["admin_user"].id,
        )


def test_issue_return_and_member_tier_loan_limits(db_env):
    _, copies = _seed_sample_book_and_copies(db_env, copy_count=3)
    b1, _, _ = db_env["branches"]
    member = db_env["members"].register_member(
        full_name="Ananya Srivastava",
        email="ananya@example.org",
        phone="+91-9123456780",
        address="Hazratganj, Lucknow",
        tier_code="BASIC",  # max 2 loans
        home_branch_id=b1.id,
        expiry_date=date(2027, 10, 24),
        actor_user_id=db_env["librarian_user"].id,
    )

    checkout_ts = datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)
    loan1 = db_env["circulation"].issue_copy(
        member_identifier=member.member_code,
        copy_identifier=copies[0].accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        checkout_at=checkout_ts,
    )
    loan2 = db_env["circulation"].issue_copy(
        member_identifier=member.member_code,
        copy_identifier=copies[1].accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        checkout_at=checkout_ts,
    )
    assert loan1.due_date == date(2026, 10, 15)
    assert loan2.status == LoanStatus.ACTIVE.value

    # Third loan blocked by BASIC max_active_loans=2
    with pytest.raises(BorrowingLimitExceededError):
        db_env["circulation"].issue_copy(
            member_identifier=member.member_code,
            copy_identifier=copies[2].accession_number,
            issuing_branch_id=b1.id,
            actor_user_id=db_env["librarian_user"].id,
            checkout_at=checkout_ts,
        )

    # Return loan1 on time -> slot freed, zero fine
    ret_outcome = db_env["circulation"].return_copy(
        copy_identifier=copies[0].accession_number,
        returned_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        returned_at=datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc),
    )
    assert ret_outcome.loan.status == LoanStatus.RETURNED.value
    assert ret_outcome.loan.copy.status == CopyStatus.AVAILABLE.value
    assert ret_outcome.fine is None


def test_suspended_member_blocked_from_checkout(db_env):
    _, copies = _seed_sample_book_and_copies(db_env, copy_count=1, isbn=None)
    b1, _, _ = db_env["branches"]
    member = db_env["members"].register_member(
        full_name="Suspended Patron",
        email="suspended@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        expiry_date=date(2027, 10, 24),
        actor_user_id=db_env["librarian_user"].id,
    )
    db_env["members"].change_membership_status(
        member.id,
        MembershipStatus.SUSPENDED,
        reason="Card reported misused",
        actor_user_id=db_env["librarian_user"].id,
    )
    with pytest.raises(MemberNotEligibleError):
        db_env["circulation"].issue_copy(
            member_identifier=member.member_code,
            copy_identifier=copies[0].accession_number,
            issuing_branch_id=b1.id,
            actor_user_id=db_env["librarian_user"].id,
        )


def test_fifo_reservation_queue_allocation_pickup_blocking_and_expiry(db_env):
    book, copies = _seed_sample_book_and_copies(
        db_env, copy_count=1, isbn="9780140449136"
    )
    copy = copies[0]
    b1, _, _ = db_env["branches"]

    m1 = db_env["members"].register_member(
        full_name="Borrower One",
        email="m1@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )
    m2 = db_env["members"].register_member(
        full_name="Waiter First",
        email="m2@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )
    m3 = db_env["members"].register_member(
        full_name="Waiter Second",
        email="m3@example.org",
        tier_code="PREMIUM",
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )

    # Reservation rejected when copy is AVAILABLE on shelf
    with pytest.raises(ReservationConflictError):
        db_env["reservations"].create_reservation(
            member_identifier=m2.member_code,
            book_id=book.id,
            preferred_branch_id=b1.id,
            actor_user_id=db_env["librarian_user"].id,
        )

    # Issue the sole copy to m1
    t0 = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
    loan1 = db_env["circulation"].issue_copy(
        member_identifier=m1.member_code,
        copy_identifier=copy.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        checkout_at=t0,
    )

    # Place two reservations in strict FIFO sequence (m2 first, m3 second)
    r_m2 = db_env["reservations"].create_reservation(
        member_identifier=m2.member_code,
        book_id=book.id,
        preferred_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        created_at=t0 + timedelta(hours=1),
    )
    r_m3 = db_env["reservations"].create_reservation(
        member_identifier=m3.member_code,
        book_id=book.id,
        preferred_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        created_at=t0 + timedelta(hours=2),
    )
    assert db_env["reservations"].get_queue_position(r_m2.id) == 1
    assert db_env["reservations"].get_queue_position(r_m3.id) == 2

    # Renewal of loan1 must be denied because m2/m3 hold active reservations
    with pytest.raises(ReservationConflictError):
        db_env["circulation"].renew_loan(
            loan1.loan_number,
            actor_user_id=db_env["librarian_user"].id,
            renewed_at=t0 + timedelta(days=10),
        )

    # Return copy -> automatically allocated to m2 (FIFO #1), copy becomes RESERVED
    ret_time = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
    ret = db_env["circulation"].return_copy(
        copy_identifier=copy.accession_number,
        returned_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        returned_at=ret_time,
    )
    assert ret.loan.copy.status == CopyStatus.RESERVED.value
    assert ret.allocated_reservation is not None
    assert ret.allocated_reservation.id == r_m2.id
    assert ret.allocated_reservation.status == ReservationStatus.READY_FOR_PICKUP.value

    # m3 CANNOT check out the copy while reserved for m2
    with pytest.raises(CopyReservedError):
        db_env["circulation"].issue_copy(
            member_identifier=m3.member_code,
            copy_identifier=copy.accession_number,
            issuing_branch_id=b1.id,
            actor_user_id=db_env["librarian_user"].id,
            checkout_at=ret_time + timedelta(hours=2),
        )

    # Simulate pickup window expiring without m2 picking up -> copy reallocates to m3!
    after_deadline = ret.allocated_reservation.pickup_deadline_at + timedelta(hours=2)
    expired_list = db_env["reservations"].process_expired_reservations(
        as_of_utc=after_deadline,
        actor_user_id=db_env["librarian_user"].id,
    )
    assert len(expired_list) == 1
    assert expired_list[0].id == r_m2.id
    assert expired_list[0].status == ReservationStatus.EXPIRED.value

    # Running expiration again at same timestamp is idempotent
    repeat_list = db_env["reservations"].process_expired_reservations(
        as_of_utc=after_deadline,
        actor_user_id=db_env["librarian_user"].id,
    )
    assert len(repeat_list) == 0

    # Now m3 can check out the reserved copy, fulfilling r_m3!
    loan_m3 = db_env["circulation"].issue_copy(
        member_identifier=m3.member_code,
        copy_identifier=copy.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        checkout_at=after_deadline + timedelta(hours=1),
    )
    assert loan_m3.member_id == m3.id
    assert (
        db_env["reservations"].get_reservation(r_m3.id).status
        == ReservationStatus.FULFILLED.value
    )


def test_damaged_return_and_lost_copy_workflows(db_env):
    book, copies = _seed_sample_book_and_copies(
        db_env, copy_count=2, isbn="9780262033848"
    )
    b1, _, _ = db_env["branches"]
    m1 = db_env["members"].register_member(
        full_name="Reader A",
        email="ra@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )
    m2 = db_env["members"].register_member(
        full_name="Reader B",
        email="rb@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )

    db_env["circulation"].issue_copy(
        member_identifier=m1.member_code,
        copy_identifier=copies[0].accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    l2 = db_env["circulation"].issue_copy(
        member_identifier=m1.member_code,
        copy_identifier=copies[1].accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    res = db_env["reservations"].create_reservation(
        member_identifier=m2.member_code,
        book_id=book.id,
        preferred_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )

    # Copy 0 returned DAMAGED -> must become DAMAGED and NOT allocate m2's reservation
    dmg_outcome = db_env["circulation"].return_copy(
        copy_identifier=copies[0].accession_number,
        returned_branch_id=b1.id,
        returned_damaged=True,
        return_notes="Water damage on cover",
        actor_user_id=db_env["librarian_user"].id,
    )
    assert dmg_outcome.loan.copy.status == CopyStatus.DAMAGED.value
    assert dmg_outcome.allocated_reservation is None

    # Copy 1 reported LOST -> loan becomes LOST, copy becomes LOST
    lost_loan, _ = db_env["circulation"].report_loan_copy_lost(
        l2.id,
        notes="Lost during travel",
        actor_user_id=db_env["librarian_user"].id,
    )
    assert lost_loan.status == LoanStatus.LOST.value

    # Damaged copy cannot be issued until reinstated
    with pytest.raises(CopyUnavailableError):
        db_env["circulation"].issue_copy(
            member_identifier=m2.member_code,
            copy_identifier=copies[0].accession_number,
            issuing_branch_id=b1.id,
            actor_user_id=db_env["librarian_user"].id,
        )

    # Admin audited reinstatement brings copy 0 back to AVAILABLE
    reinstated = db_env["admin"].admin_reinstate_withdrawn_copy(
        copies[0].id,
        reason="Rebound by conservation unit",
        actor_user_id=db_env["admin_user"].id,
    )
    assert reinstated.status == CopyStatus.AVAILABLE.value
    assert res.status == ReservationStatus.WAITING.value

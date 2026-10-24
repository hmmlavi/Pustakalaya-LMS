"""Integration tests for historical data import validation, quarantine, and idempotency."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from sqlalchemy import func, select

from app.infrastructure.transactions import read_session
from app.models import Fine, FinePayment, ImportIssue, Loan, Reservation


def test_historical_import_accepts_valid_quarantines_inconsistent_and_is_idempotent(
    db_env, tmp_path: Path
):
    b1, b2, _ = db_env["branches"]
    book = db_env["books"].create_book(
        title="Awadh History Compendium",
        isbn="9780306406157",
        publisher="Oxford University Press India",
        publication_year=2018,
        language="English",
        category="History & Archaeology",
        edition="1st",
        author_names=["Rosie Llewellyn-Jones"],
        actor_user_id=db_env["admin_user"].id,
    )
    c1 = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-IMP-01",
        acquisition_date=date(2024, 1, 10),
        actor_user_id=db_env["admin_user"].id,
    )
    c2 = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b2.id,
        accession_number="LKO-GMN-IMP-02",
        acquisition_date=date(2024, 1, 10),
        actor_user_id=db_env["admin_user"].id,
    )
    member = db_env["members"].register_member(
        full_name="Import Test Patron",
        email="import.patron@example.org",
        tier_code="BASIC",  # max_renewals=1, ₹5/day, grace 1d
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )

    records = [
        # 1. Valid returned overdue loan with matching fine & partial payment
        # Due 2026-03-15, returned 2026-03-20 -> 5 days - 1 grace = 4 days * ₹5 = ₹20.00
        {
            "source_id": "SRC-LOAN-VALID-1",
            "record_type": "LOAN",
            "member_code": member.member_code,
            "copy_accession": c1.accession_number,
            "branch_code": "LKO-HZR",
            "checkout_date": "2026-03-01",
            "due_date": "2026-03-15",
            "return_date": "2026-03-20",
            "status": "RETURNED",
            "renewal_count": 0,
            "fine": {
                "assessed_amount": "20.00",
                "paid_amount": "15.00",
                "payment_method": "UPI",
            },
        },
        # 2. Valid active loan on c2
        {
            "source_id": "SRC-LOAN-ACTIVE-1",
            "record_type": "LOAN",
            "member_code": member.member_code,
            "copy_accession": c2.accession_number,
            "branch_code": "LKO-GMN",
            "checkout_date": "2026-10-15",
            "due_date": "2026-10-29",
            "return_date": None,
            "status": "ACTIVE",
            "renewal_count": 0,
        },
        # 3. Duplicate source_id -> DUPLICATE_SOURCE_ID (skipped)
        {
            "source_id": "SRC-LOAN-VALID-1",
            "record_type": "LOAN",
            "member_code": member.member_code,
            "copy_accession": c1.accession_number,
            "branch_code": "LKO-HZR",
            "checkout_date": "2026-03-01",
            "due_date": "2026-03-15",
            "return_date": "2026-03-20",
            "status": "RETURNED",
            "renewal_count": 0,
        },
        # 4. Malformed ISBN book -> MALFORMED_ISBN (rejected)
        {
            "source_id": "SRC-BAD-ISBN-1",
            "record_type": "BOOK",
            "title": "Corrupt ISBN Book",
            "isbn": "978-0-306-40615-0",
            "publisher": "Legacy Press",
            "publication_year": 2010,
            "language": "English",
            "category": "History",
            "authors": ["Unknown"],
        },
        # 5. Nonexistent member -> INVALID_MEMBER_REFERENCE (rejected)
        {
            "source_id": "SRC-GHOST-MEM-1",
            "record_type": "LOAN",
            "member_code": "MEM-999999",
            "copy_accession": c1.accession_number,
            "branch_code": "LKO-HZR",
            "checkout_date": "2026-04-01",
            "due_date": "2026-04-15",
            "return_date": "2026-04-12",
            "status": "RETURNED",
            "renewal_count": 0,
        },
        # 6. Nonexistent branch -> INVALID_BRANCH_REFERENCE (rejected)
        {
            "source_id": "SRC-GHOST-BR-1",
            "record_type": "LOAN",
            "member_code": member.member_code,
            "copy_accession": c1.accession_number,
            "branch_code": "LKO-GHOST",
            "checkout_date": "2026-04-20",
            "due_date": "2026-05-04",
            "return_date": "2026-05-01",
            "status": "RETURNED",
            "renewal_count": 0,
        },
        # 7. Missing return date on RETURNED loan -> MISSING_RETURN_DATE (quarantined)
        {
            "source_id": "SRC-MISSING-RET-1",
            "record_type": "LOAN",
            "member_code": member.member_code,
            "copy_accession": c1.accession_number,
            "branch_code": "LKO-HZR",
            "checkout_date": "2026-05-10",
            "due_date": "2026-05-24",
            "return_date": None,
            "status": "RETURNED",
            "renewal_count": 0,
        },
        # 8. Inverted return < checkout -> RETURN_BEFORE_CHECKOUT (rejected)
        {
            "source_id": "SRC-INVERTED-1",
            "record_type": "LOAN",
            "member_code": member.member_code,
            "copy_accession": c1.accession_number,
            "branch_code": "LKO-HZR",
            "checkout_date": "2026-06-10",
            "due_date": "2026-06-24",
            "return_date": "2026-06-05",
            "status": "RETURNED",
            "renewal_count": 0,
        },
        # 9. Conflicting active loan on c2 -> CONFLICTING_ACTIVE_LOAN (rejected)
        {
            "source_id": "SRC-CONFLICT-ACTIVE-1",
            "record_type": "LOAN",
            "member_code": member.member_code,
            "copy_accession": c2.accession_number,
            "branch_code": "LKO-GMN",
            "checkout_date": "2026-10-18",
            "due_date": "2026-11-01",
            "return_date": None,
            "status": "ACTIVE",
            "renewal_count": 0,
        },
        # 10. Renewal count exceeds BASIC max_renewals=1 -> EXCESSIVE_RENEWAL_COUNT (quarantined)
        {
            "source_id": "SRC-RENEW-OVER-1",
            "record_type": "LOAN",
            "member_code": member.member_code,
            "copy_accession": c1.accession_number,
            "branch_code": "LKO-HZR",
            "checkout_date": "2026-07-01",
            "due_date": "2026-07-29",
            "return_date": "2026-07-25",
            "status": "RETURNED",
            "renewal_count": 5,
        },
        # 11. Negative fine amount -> NEGATIVE_FINE_AMOUNT (rejected)
        {
            "source_id": "SRC-NEG-FINE-1",
            "record_type": "LOAN",
            "member_code": member.member_code,
            "copy_accession": c1.accession_number,
            "branch_code": "LKO-HZR",
            "checkout_date": "2026-08-01",
            "due_date": "2026-08-15",
            "return_date": "2026-08-20",
            "status": "RETURNED",
            "renewal_count": 0,
            "fine": {"assessed_amount": "-10.00", "paid_amount": "0.00"},
        },
        # 12. Unexplained fine discrepancy -> UNEXPLAINED_FINE_DISCREPANCY (quarantined)
        {
            "source_id": "SRC-FINE-MISMATCH-1",
            "record_type": "LOAN",
            "member_code": member.member_code,
            "copy_accession": c1.accession_number,
            "branch_code": "LKO-HZR",
            "checkout_date": "2026-09-01",
            "due_date": "2026-09-15",
            "return_date": "2026-09-20",
            "status": "RETURNED",
            "renewal_count": 0,
            "fine": {"assessed_amount": "99.00", "paid_amount": "0.00"},
        },
        # 13. Valid historical fulfilled reservation
        {
            "source_id": "SRC-RES-VALID-1",
            "record_type": "RESERVATION",
            "member_code": member.member_code,
            "book_isbn": "9780306406157",
            "branch_code": "LKO-HZR",
            "status": "FULFILLED",
            "created_date": "2026-02-10",
        },
    ]

    fixture_file = tmp_path / "curated_history.json"
    fixture_file.write_text(json.dumps({"records": records}, indent=2), encoding="utf-8")

    batch1 = db_env["imports"].import_from_file(
        fixture_file, actor_user_id=db_env["admin_user"].id
    )
    assert batch1.total_records == 13
    assert batch1.accepted_count == 3
    assert batch1.duplicate_skipped_count == 1
    assert batch1.quarantined_count == 3
    assert batch1.rejected_count == 6
    assert batch1.error_count == 0

    with read_session(db_env["sf"]) as session:
        assert session.scalar(select(func.count(Loan.id))) == 2
        assert session.scalar(select(func.count(Fine.id))) == 1
        assert session.scalar(select(func.count(FinePayment.id))) == 1
        assert session.scalar(select(func.count(Reservation.id))) == 1
        logged_codes = set(session.scalars(select(ImportIssue.issue_code)).all())
        assert {
            "DUPLICATE_SOURCE_ID",
            "MALFORMED_ISBN",
            "INVALID_MEMBER_REFERENCE",
            "INVALID_BRANCH_REFERENCE",
            "MISSING_RETURN_DATE",
            "RETURN_BEFORE_CHECKOUT",
            "CONFLICTING_ACTIVE_LOAN",
            "EXCESSIVE_RENEWAL_COUNT",
            "NEGATIVE_FINE_AMOUNT",
            "UNEXPLAINED_FINE_DISCREPANCY",
        }.issubset(logged_codes)

    # Re-run exact same import file -> 100% idempotent (0 newly accepted, operational counts unchanged)
    batch2 = db_env["imports"].import_from_file(
        fixture_file, actor_user_id=db_env["admin_user"].id
    )
    assert batch2.accepted_count == 0
    assert batch2.duplicate_skipped_count >= 3

    with read_session(db_env["sf"]) as session:
        assert session.scalar(select(func.count(Loan.id))) == 2
        assert session.scalar(select(func.count(Fine.id))) == 1
        assert session.scalar(select(func.count(FinePayment.id))) == 1
        assert session.scalar(select(func.count(Reservation.id))) == 1

"""Tests for fines, partial payments, SQLite double-checkout concurrency, and atomic rollback."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
import threading

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.domain.enums import CopyStatus, FineStatus, PaymentMethod
from app.domain.exceptions import (
    DomainError,
    InvalidFinePaymentError,
    OutstandingFineError,
)
from app.infrastructure.transactions import read_session
from app.models import AuditEvent, BookCopy, Loan


def test_overdue_fine_partial_and_full_payments_blocking_and_waiver(db_env):
    b1, _, _ = db_env["branches"]
    book = db_env["books"].create_book(
        title="Raag Darbari",
        isbn=None,
        publisher="Rajkamal Prakashan",
        publication_year=1968,
        language="Hindi",
        category="Hindi Literature",
        edition="Paperback",
        author_names=["Shrilal Shukla"],
        actor_user_id=db_env["admin_user"].id,
    )
    c1 = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-FINE-01",
        acquisition_date=date(2025, 1, 1),
        actor_user_id=db_env["admin_user"].id,
    )
    c2 = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-FINE-02",
        acquisition_date=date(2025, 1, 1),
        actor_user_id=db_env["admin_user"].id,
    )
    member = db_env["members"].register_member(
        full_name="Vikram Verma",
        email="vikram@example.org",
        tier_code="BASIC",  # ₹5/day, grace 1d, blocking threshold ₹50.00
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )

    # Issue Oct 1 -> due Oct 15. Return Oct 27 -> 12 chargeable days - 1 grace day = 11 days * ₹5 = ₹55.00 (>= ₹50 threshold)
    db_env["circulation"].issue_copy(
        member_identifier=member.member_code,
        copy_identifier=c1.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        checkout_at=datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc),
    )
    ret = db_env["circulation"].return_copy(
        copy_identifier=c1.accession_number,
        returned_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        returned_at=datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc),
    )
    assert ret.fine is not None
    assert ret.fine.assessed_amount == Decimal("55.00")
    assert ret.fine.status == FineStatus.UNPAID.value

    # Member is blocked from borrowing c2 because ₹55.00 >= ₹50.00 threshold
    with pytest.raises(OutstandingFineError):
        db_env["circulation"].issue_copy(
            member_identifier=member.member_code,
            copy_identifier=c2.accession_number,
            issuing_branch_id=b1.id,
            actor_user_id=db_env["librarian_user"].id,
            checkout_at=datetime(2026, 10, 27, 11, 0, tzinfo=timezone.utc),
        )

    # Reject overpayment
    with pytest.raises(InvalidFinePaymentError):
        db_env["fines"].record_payment(
            ret.fine.id,
            amount="75.00",
            payment_method=PaymentMethod.CASH,
            branch_id=b1.id,
            actor_user_id=db_env["librarian_user"].id,
        )

    # Partial payment of ₹20.00 -> remaining ₹35.00 (< ₹50.00 threshold), status PARTIALLY_PAID
    p1 = db_env["fines"].record_payment(
        ret.fine.id,
        amount="20.00",
        payment_method=PaymentMethod.UPI,
        branch_id=b1.id,
        idempotency_key="IDEMP-TEST-001",
        actor_user_id=db_env["librarian_user"].id,
    )
    updated_fine = db_env["fines"].get_fine(ret.fine.id)
    assert updated_fine.status == FineStatus.PARTIALLY_PAID.value
    assert updated_fine.outstanding_balance == Decimal("35.00")

    # Exact retry with same idempotency_key returns existing receipt without double-charging
    p1_retry = db_env["fines"].record_payment(
        ret.fine.id,
        amount="20.00",
        payment_method=PaymentMethod.UPI,
        branch_id=b1.id,
        idempotency_key="IDEMP-TEST-001",
        actor_user_id=db_env["librarian_user"].id,
    )
    assert p1_retry.receipt_number == p1.receipt_number
    assert db_env["fines"].get_fine(ret.fine.id).outstanding_balance == Decimal("35.00")

    # Reusing same idempotency_key for a different amount is rejected
    with pytest.raises(InvalidFinePaymentError):
        db_env["fines"].record_payment(
            ret.fine.id,
            amount="10.00",
            payment_method=PaymentMethod.UPI,
            branch_id=b1.id,
            idempotency_key="IDEMP-TEST-001",
            actor_user_id=db_env["librarian_user"].id,
        )

    # Member can now borrow c2 because balance ₹35.00 < ₹50.00
    loan2 = db_env["circulation"].issue_copy(
        member_identifier=member.member_code,
        copy_identifier=c2.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        checkout_at=datetime(2026, 10, 27, 11, 30, tzinfo=timezone.utc),
    )
    assert loan2.id is not None

    # Settle ₹15.00 more, then admin waives remaining ₹20.00 -> status PAID, assessed_amount preserved!
    db_env["fines"].record_payment(
        ret.fine.id,
        amount="15.00",
        payment_method=PaymentMethod.CASH,
        branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    waived = db_env["fines"].waive_fine(
        ret.fine.id,
        reason="Goodwill waiver for first-time confusion",
        actor_user_id=db_env["admin_user"].id,
    )
    assert waived.assessed_amount == Decimal("55.00")
    assert waived.paid_amount == Decimal("35.00")
    assert waived.waived_amount == Decimal("20.00")
    assert waived.outstanding_balance == Decimal("0.00")
    assert waived.status == FineStatus.PAID.value


def test_sqlite_concurrent_double_checkout_allows_only_one_winner(db_env):
    """Two simultaneous checkout attempts for the same physical copy on SQLite."""
    b1, _, _ = db_env["branches"]
    book = db_env["books"].create_book(
        title="Concurrency Under Contention",
        isbn=None,
        publisher="IIT Press",
        publication_year=2022,
        language="English",
        category="Computer Science & Engineering",
        edition="1st",
        author_names=["Raghu Ramakrishnan"],
        actor_user_id=db_env["admin_user"].id,
    )
    copy = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-RACE-001",
        acquisition_date=date(2025, 5, 1),
        actor_user_id=db_env["admin_user"].id,
    )
    m_a = db_env["members"].register_member(
        full_name="Concurrent Reader A",
        email="ca@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )
    m_b = db_env["members"].register_member(
        full_name="Concurrent Reader B",
        email="cb@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )

    barrier = threading.Barrier(2)

    def attempt_checkout(member_code: str):
        barrier.wait(timeout=5)
        return db_env["circulation"].issue_copy(
            member_identifier=member_code,
            copy_identifier=copy.accession_number,
            issuing_branch_id=b1.id,
            actor_user_id=db_env["librarian_user"].id,
        )

    successes = []
    failures = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(attempt_checkout, m_a.member_code),
            pool.submit(attempt_checkout, m_b.member_code),
        ]
        for fut in futures:
            try:
                successes.append(fut.result())
            except DomainError as exc:
                failures.append(exc)

    assert len(successes) == 1
    assert len(failures) == 1

    with read_session(db_env["sf"]) as session:
        active_loans = session.scalars(
            select(Loan).where(Loan.copy_id == copy.id, Loan.status == "ACTIVE")
        ).all()
        persisted_copy = session.get(BookCopy, copy.id)
        assert len(active_loans) == 1
        assert persisted_copy.status == CopyStatus.ON_LOAN.value


def test_atomic_rollback_prevents_partial_writes_and_audit_leak(db_env):
    """Simulate mid-transaction failure during issue_copy and return_copy."""
    b1, _, _ = db_env["branches"]
    book = db_env["books"].create_book(
        title="Atomic Rollback Proof",
        isbn=None,
        publisher="Academic Press",
        publication_year=2023,
        language="English",
        category="Mathematics & Logic",
        edition="1st",
        author_names=["Harish-Chandra"],
        actor_user_id=db_env["admin_user"].id,
    )
    copy = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-ROLLBACK-01",
        acquisition_date=date(2025, 6, 1),
        actor_user_id=db_env["admin_user"].id,
    )
    member = db_env["members"].register_member(
        full_name="Rollback Reader",
        email="rollback@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )

    with read_session(db_env["sf"]) as session:
        audit_before = session.scalar(select(func.count(AuditEvent.id)))
        loans_before = session.scalar(select(func.count(Loan.id)))

    def explode_before_commit():
        raise RuntimeError("Simulated mid-transaction crash before commit")

    with pytest.raises(RuntimeError, match="Simulated mid-transaction crash"):
        db_env["circulation"].issue_copy(
            member_identifier=member.member_code,
            copy_identifier=copy.accession_number,
            issuing_branch_id=b1.id,
            actor_user_id=db_env["librarian_user"].id,
            _before_commit_hook=explode_before_commit,
        )

    # Verify zero loan rows, zero audit events leaked, and copy remains AVAILABLE
    with read_session(db_env["sf"]) as session:
        assert session.scalar(select(func.count(AuditEvent.id))) == audit_before
        assert session.scalar(select(func.count(Loan.id))) == loans_before
        assert session.get(BookCopy, copy.id).status == CopyStatus.AVAILABLE.value

    # Now issue normally, then fail mid-way through overdue return (Fine + Copy + Audit)
    db_env["circulation"].issue_copy(
        member_identifier=member.member_code,
        copy_identifier=copy.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        checkout_at=datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
    )
    with read_session(db_env["sf"]) as session:
        audit_after_issue = session.scalar(select(func.count(AuditEvent.id)))

    with pytest.raises(RuntimeError, match="Simulated mid-transaction crash"):
        db_env["circulation"].return_copy(
            copy_identifier=copy.accession_number,
            returned_branch_id=b1.id,
            actor_user_id=db_env["librarian_user"].id,
            returned_at=datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc),
            _before_commit_hook=explode_before_commit,
        )

    with read_session(db_env["sf"]) as session:
        assert session.scalar(select(func.count(AuditEvent.id))) == audit_after_issue
        assert session.get(BookCopy, copy.id).status == CopyStatus.ON_LOAN.value


def test_sqlite_foreign_key_pragma_is_strictly_enforced(db_env):
    """Ensure SQLite connection event listener enables PRAGMA foreign_keys = ON."""
    with pytest.raises(IntegrityError):
        with db_env["sf"]() as session:
            session.add(
                BookCopy(
                    accession_number="ORPHAN-COPY-999",
                    book_id=999999,
                    owning_branch_id=999999,
                    status=CopyStatus.AVAILABLE.value,
                    acquisition_date=date(2026, 1, 1),
                )
            )
            session.commit()

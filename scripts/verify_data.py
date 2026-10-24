"""Comprehensive database integrity, migration, and dataset verification script."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import BASE_DIR, AppConfig
from app.domain.enums import CopyStatus, LoanStatus, ReservationStatus
from app.infrastructure.database import create_db_engine, create_session_factory
from app.infrastructure.seed import (
    EXPECTED_BOOK_COUNT,
    EXPECTED_BRANCH_COUNT,
    EXPECTED_MEMBER_COUNT,
)
from app.infrastructure.transactions import read_session
from app.models import (
    AuditEvent,
    Author,
    Book,
    BookCopy,
    Fine,
    FinePayment,
    ImportBatch,
    LibraryBranch,
    LibraryHoliday,
    Loan,
    Member,
    MembershipTier,
    Reservation,
    User,
)


@dataclass(frozen=True, slots=True)
class IntegrityVerificationReport:
    passed: bool
    current_revision: str | None
    expected_head_revision: str | None
    sqlite_integrity_ok: bool
    foreign_key_violations: int
    counts: dict[str, int]
    invariant_violations: dict[str, int]
    latest_import: dict[str, Any] | None


def run_verification(database_url: str) -> IntegrityVerificationReport:
    engine = create_db_engine(database_url)
    try:
        script_dir = ScriptDirectory(str(BASE_DIR / "migrations"))
        expected_head = script_dir.get_current_head()

        with engine.connect() as conn:
            mig_ctx = MigrationContext.configure(conn)
            current_rev = mig_ctx.get_current_revision()
            fk_rows = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            integ_row = conn.exec_driver_sql("PRAGMA integrity_check").fetchone()
            sqlite_ok = bool(integ_row and integ_row[0] == "ok")

        session_factory = create_session_factory(engine)
        with read_session(session_factory) as session:
            counts = {
                "branches": int(
                    session.scalar(select(func.count(LibraryBranch.id))) or 0
                ),
                "membership_tiers": int(
                    session.scalar(select(func.count(MembershipTier.id))) or 0
                ),
                "holidays": int(
                    session.scalar(select(func.count(LibraryHoliday.id))) or 0
                ),
                "authors": int(session.scalar(select(func.count(Author.id))) or 0),
                "books": int(session.scalar(select(func.count(Book.id))) or 0),
                "book_copies": int(
                    session.scalar(select(func.count(BookCopy.id))) or 0
                ),
                "members": int(session.scalar(select(func.count(Member.id))) or 0),
                "users": int(session.scalar(select(func.count(User.id))) or 0),
                "loans_total": int(session.scalar(select(func.count(Loan.id))) or 0),
                "loans_active": int(
                    session.scalar(
                        select(func.count(Loan.id)).where(
                            Loan.status == LoanStatus.ACTIVE.value
                        )
                    )
                    or 0
                ),
                "loans_returned": int(
                    session.scalar(
                        select(func.count(Loan.id)).where(
                            Loan.status == LoanStatus.RETURNED.value
                        )
                    )
                    or 0
                ),
                "reservations": int(
                    session.scalar(select(func.count(Reservation.id))) or 0
                ),
                "fines": int(session.scalar(select(func.count(Fine.id))) or 0),
                "fine_payments": int(
                    session.scalar(select(func.count(FinePayment.id))) or 0
                ),
                "audit_events": int(
                    session.scalar(select(func.count(AuditEvent.id))) or 0
                ),
            }

            dup_accessions = int(
                session.scalar(
                    select(func.count()).select_from(
                        select(BookCopy.accession_number)
                        .group_by(BookCopy.accession_number)
                        .having(func.count(BookCopy.id) > 1)
                        .subquery()
                    )
                )
                or 0
            )
            dup_active_loans = int(
                session.scalar(
                    select(func.count()).select_from(
                        select(Loan.copy_id)
                        .where(Loan.status == LoanStatus.ACTIVE.value)
                        .group_by(Loan.copy_id)
                        .having(func.count(Loan.id) > 1)
                        .subquery()
                    )
                )
                or 0
            )
            active_loan_wrong_copy_state = int(
                session.scalar(
                    select(func.count(Loan.id))
                    .join(BookCopy, BookCopy.id == Loan.copy_id)
                    .where(
                        Loan.status == LoanStatus.ACTIVE.value,
                        BookCopy.status != CopyStatus.ON_LOAN.value,
                    )
                )
                or 0
            )
            on_loan_copy_without_active_loan = int(
                session.scalar(
                    select(func.count(BookCopy.id)).where(
                        BookCopy.status == CopyStatus.ON_LOAN.value,
                        ~BookCopy.id.in_(
                            select(Loan.copy_id).where(
                                Loan.status == LoanStatus.ACTIVE.value
                            )
                        ),
                    )
                )
                or 0
            )
            ready_hold_wrong_copy_state = int(
                session.scalar(
                    select(func.count(Reservation.id))
                    .join(BookCopy, BookCopy.id == Reservation.allocated_copy_id)
                    .where(
                        Reservation.status
                        == ReservationStatus.READY_FOR_PICKUP.value,
                        BookCopy.status != CopyStatus.RESERVED.value,
                    )
                )
                or 0
            )
            reserved_copy_without_ready_hold = int(
                session.scalar(
                    select(func.count(BookCopy.id)).where(
                        BookCopy.status == CopyStatus.RESERVED.value,
                        ~BookCopy.id.in_(
                            select(Reservation.allocated_copy_id).where(
                                Reservation.status
                                == ReservationStatus.READY_FOR_PICKUP.value,
                                Reservation.allocated_copy_id.is_not(None),
                            )
                        ),
                    )
                )
                or 0
            )
            dup_isbns = int(
                session.scalar(
                    select(func.count()).select_from(
                        select(Book.isbn)
                        .where(Book.isbn.is_not(None))
                        .group_by(Book.isbn)
                        .having(func.count(Book.id) > 1)
                        .subquery()
                    )
                )
                or 0
            )
            invalid_fine_balances = int(
                session.scalar(
                    text(
                        "SELECT COUNT(*) FROM fines WHERE assessed_amount < 0 OR paid_amount < 0 "
                        "OR waived_amount < 0 OR (paid_amount + waived_amount) > assessed_amount"
                    )
                )
                or 0
            )
            fine_payment_sum_mismatches = int(
                session.scalar(
                    text(
                        "SELECT COUNT(*) FROM fines f WHERE f.paid_amount != "
                        "COALESCE((SELECT SUM(fp.amount) FROM fine_payments fp WHERE fp.fine_id = f.id), 0)"
                    )
                )
                or 0
            )
            fine_status_mismatches = int(
                session.scalar(
                    text(
                        """
                        SELECT COUNT(*) FROM fines
                        WHERE (
                            (paid_amount + waived_amount >= assessed_amount AND waived_amount > 0 AND status != 'WAIVED')
                            OR (paid_amount + waived_amount >= assessed_amount AND waived_amount = 0 AND status != 'PAID')
                            OR (paid_amount + waived_amount < assessed_amount AND paid_amount > 0 AND status != 'PARTIALLY_PAID')
                            OR (paid_amount + waived_amount < assessed_amount AND paid_amount = 0 AND waived_amount = 0 AND status != 'UNPAID')
                        )
                        """
                    )
                )
                or 0
            )

            invariants = {
                "duplicate_catalog_isbns": dup_isbns,
                "duplicate_accession_numbers": dup_accessions,
                "duplicate_active_loans_per_copy": dup_active_loans,
                "active_loans_with_non_on_loan_copy": active_loan_wrong_copy_state,
                "on_loan_copies_without_active_loan": on_loan_copy_without_active_loan,
                "ready_reservations_with_non_reserved_copy": ready_hold_wrong_copy_state,
                "reserved_copies_without_ready_reservation": reserved_copy_without_ready_hold,
                "invalid_fine_ledger_balances": invalid_fine_balances,
                "fine_payment_sum_mismatches": fine_payment_sum_mismatches,
                "fine_status_mismatches": fine_status_mismatches,
            }

            latest_batch = session.scalar(
                select(ImportBatch).order_by(ImportBatch.id.desc()).limit(1)
            )
            import_info: dict[str, Any] | None = None
            if latest_batch is not None:
                import_info = {
                    "batch_key": latest_batch.batch_key,
                    "source_filename": latest_batch.source_filename,
                    "total_records": latest_batch.total_records,
                    "accepted": latest_batch.accepted_count,
                    "rejected": latest_batch.rejected_count,
                    "quarantined": latest_batch.quarantined_count,
                    "duplicate_skipped": latest_batch.duplicate_skipped_count,
                    "errors": latest_batch.error_count,
                }

        all_ok = (
            current_rev == expected_head
            and sqlite_ok
            and len(fk_rows) == 0
            and all(v == 0 for v in invariants.values())
            and counts["branches"] == EXPECTED_BRANCH_COUNT
            and counts["books"] == EXPECTED_BOOK_COUNT
            and counts["members"] == EXPECTED_MEMBER_COUNT
        )

        return IntegrityVerificationReport(
            passed=all_ok,
            current_revision=current_rev,
            expected_head_revision=expected_head,
            sqlite_integrity_ok=sqlite_ok,
            foreign_key_violations=len(fk_rows),
            counts=counts,
            invariant_violations=invariants,
            latest_import=import_info,
        )
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Pustakalaya schema migrations, counts, foreign keys, and operational invariants."
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = AppConfig(database_url=args.database_url)
    report = run_verification(cfg.DATABASE_URL)

    if args.json:
        print(
            json.dumps(
                {
                    "passed": report.passed,
                    "migration": {
                        "current": report.current_revision,
                        "expected_head": report.expected_head_revision,
                    },
                    "sqlite_integrity_ok": report.sqlite_integrity_ok,
                    "foreign_key_violations": report.foreign_key_violations,
                    "counts": report.counts,
                    "invariant_violations": report.invariant_violations,
                    "latest_import": report.latest_import,
                },
                indent=2,
            )
        )
    else:
        print("=== Pustakalaya Database & Integrity Verification Report ===")
        print(
            f"  Migration Status   : current={report.current_revision} | head={report.expected_head_revision}"
        )
        print(f"  SQLite Integrity   : {'OK' if report.sqlite_integrity_ok else 'FAILED'}")
        print(f"  FK Violations      : {report.foreign_key_violations}")
        print("  Record Counts (Expected vs Actual):")
        print(
            f"    - Branches       : expected={EXPECTED_BRANCH_COUNT}, actual={report.counts['branches']}"
        )
        print(
            f"    - Catalog Books  : expected={EXPECTED_BOOK_COUNT}, actual={report.counts['books']}"
        )
        print(
            f"    - Members        : expected={EXPECTED_MEMBER_COUNT}, actual={report.counts['members']}"
        )
        print(f"    - Tiers          : actual={report.counts['membership_tiers']}")
        print(f"    - Holidays       : actual={report.counts['holidays']}")
        print(f"    - Authors        : actual={report.counts['authors']}")
        print(f"    - Book Copies    : actual={report.counts['book_copies']}")
        print(
            f"    - Loans          : total={report.counts['loans_total']} (active={report.counts['loans_active']}, returned={report.counts['loans_returned']})"
        )
        print(f"    - Reservations   : actual={report.counts['reservations']}")
        print(
            f"    - Fines/Payments : fines={report.counts['fines']}, payments={report.counts['fine_payments']}"
        )
        print("  Operational Invariant Checks:")
        for key, val in report.invariant_violations.items():
            print(f"    - {key:<44}: {val}")
        if report.latest_import:
            print("  Latest Historical Import Batch:")
            for k, v in report.latest_import.items():
                print(f"    - {k:<22}: {v}")
        print(
            f"\nOVERALL VERIFICATION STATUS: {'PASSED' if report.passed else 'FAILED'}"
        )

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

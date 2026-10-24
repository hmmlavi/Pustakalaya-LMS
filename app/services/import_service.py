"""Historical data validation and import pipeline service.

Validates structure, foreign relationships, chronological dates, duplicates,
overlapping loan intervals, reservation conflicts, and fine/payment arithmetic.
Accepts valid records idempotently while rejecting or quarantining malformed
historical source records into `import_issues` with structured reason codes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Sequence
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import (
    AuditEventType,
    CopyStatus,
    FineStatus,
    ImportDisposition,
    LoanStatus,
    PaymentMethod,
    ReservationStatus,
)
from app.domain.exceptions import DataImportValidationError, InvalidISBNError
from app.domain.policies import PolicyFactory
from app.domain.value_objects import ISBN, ZERO_MONEY, quantize_money, utc_now
from app.infrastructure.transactions import read_session, write_transaction
from app.models import (
    Author,
    Book,
    BookCopy,
    Fine,
    FinePayment,
    ImportBatch,
    ImportIssue,
    LibraryBranch,
    Loan,
    Member,
    Reservation,
)
from app.repositories import (
    AuditRepository,
    AuthorRepository,
    BookRepository,
    BranchRepository,
    CopyRepository,
    FineRepository,
    HolidayRepository,
    ImportBatchRepository,
    LoanRepository,
    MemberRepository,
    Page,
    ReservationRepository,
)
from app.services.fine_service import FineService


@dataclass(frozen=True, slots=True)
class ImportValidationReport:
    batch_id: int
    batch_key: str
    source_filename: str
    total_records: int
    accepted_count: int
    rejected_count: int
    quarantined_count: int
    duplicate_skipped_count: int
    error_count: int
    issue_counts_by_code: dict[str, int] = field(default_factory=dict)


class _ImportDispositionSignal(Exception):
    """Internal control signal classifying a rejected, quarantined, or skipped record."""

    def __init__(
        self, disposition: ImportDisposition, code: str, message: str
    ) -> None:
        super().__init__(message)
        self.disposition = disposition
        self.code = code
        self.message = message


class DataImportService:
    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        library_timezone: str = "Asia/Kolkata",
    ) -> None:
        self.session_factory = session_factory
        self.library_timezone = library_timezone
        self.fine_service = FineService(
            session_factory, library_timezone=library_timezone
        )

    def _parse_iso_date(self, raw: Any, field_name: str) -> date:
        if not raw or not isinstance(raw, str):
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "MISSING_REQUIRED_FIELD",
                f"Missing required date field '{field_name}'.",
            )
        try:
            return date.fromisoformat(raw.strip()[:10])
        except ValueError as exc:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "INVALID_DATE_FORMAT",
                f"Field '{field_name}' has invalid ISO date '{raw}'.",
            ) from exc

    def _local_day_noon_utc(self, d: date) -> datetime:
        local_dt = datetime.combine(
            d, time(12, 0, 0), tzinfo=ZoneInfo(self.library_timezone)
        )
        return local_dt.astimezone(timezone.utc)

    def import_from_file(
        self,
        file_path: Path | str,
        *,
        actor_user_id: int | None = None,
    ) -> ImportValidationReport:
        path = Path(file_path)
        if not path.exists():
            raise DataImportValidationError(
                f"Historical source file '{path}' does not exist."
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DataImportValidationError(
                f"Historical source file '{path.name}' is not valid JSON: {exc}"
            ) from exc

        if isinstance(payload, dict) and "records" in payload:
            records = payload["records"]
        elif isinstance(payload, list):
            records = payload
        else:
            raise DataImportValidationError(
                "Historical JSON payload must be a list of records or an object with a 'records' array."
            )

        if not isinstance(records, list):
            raise DataImportValidationError("'records' must be a JSON array.")

        return self.import_records(
            records,
            source_filename=path.name,
            actor_user_id=actor_user_id,
        )

    def import_records(
        self,
        records: Sequence[dict[str, Any]],
        *,
        source_filename: str = "inline_records.json",
        actor_user_id: int | None = None,
    ) -> ImportValidationReport:
        batch_key = f"IMP-{utc_now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
        accepted = 0
        rejected = 0
        quarantined = 0
        duplicates = 0
        errors = 0
        issue_code_counts: dict[str, int] = {}

        with write_transaction(self.session_factory) as session:
            batch_repo = ImportBatchRepository(session)
            batch = ImportBatch(
                batch_key=batch_key,
                source_filename=source_filename,
                total_records=len(records),
                initiated_by_user_id=actor_user_id,
                started_at=utc_now(),
            )
            batch_repo.add_batch(batch)

            seen_source_ids: set[str] = set()
            calendar = HolidayRepository(session).build_calendar()

            for idx, rec in enumerate(records, start=1):
                if not isinstance(rec, dict):
                    rejected += 1
                    code = "MALFORMED_RECORD_STRUCTURE"
                    issue_code_counts[code] = issue_code_counts.get(code, 0) + 1
                    batch_repo.add_issue(
                        ImportIssue(
                            batch_id=batch.id,
                            source_record_id=f"ROW-{idx}",
                            record_type="UNKNOWN",
                            disposition=ImportDisposition.REJECTED.value,
                            issue_code=code,
                            message="Source record is not a JSON object.",
                            raw_payload_json=json.dumps(rec, default=str),
                        )
                    )
                    continue

                raw_source_id = str(rec.get("source_id") or "").strip()
                rec_type = str(rec.get("record_type") or "UNKNOWN").strip().upper()
                display_id = raw_source_id or f"ROW-{idx}"

                try:
                    with session.begin_nested():
                        if not raw_source_id:
                            raise _ImportDispositionSignal(
                                ImportDisposition.REJECTED,
                                "MISSING_SOURCE_ID",
                                "Record is missing required 'source_id' identifier.",
                            )
                        if raw_source_id in seen_source_ids:
                            raise _ImportDispositionSignal(
                                ImportDisposition.DUPLICATE_SKIPPED,
                                "DUPLICATE_SOURCE_ID",
                                f"Duplicate source_id '{raw_source_id}' within import payload.",
                            )
                        seen_source_ids.add(raw_source_id)

                        if rec_type == "BOOK":
                            self._import_book_record(session, raw_source_id, rec)
                        elif rec_type == "LOAN":
                            self._import_loan_record(
                                session, raw_source_id, rec, calendar
                            )
                        elif rec_type == "RESERVATION":
                            self._import_reservation_record(
                                session, raw_source_id, rec
                            )
                        else:
                            raise _ImportDispositionSignal(
                                ImportDisposition.REJECTED,
                                "UNSUPPORTED_RECORD_TYPE",
                                f"Unsupported historical record_type '{rec_type}'.",
                            )
                        accepted += 1

                except _ImportDispositionSignal as sig:
                    if sig.disposition == ImportDisposition.REJECTED:
                        rejected += 1
                    elif sig.disposition == ImportDisposition.QUARANTINED:
                        quarantined += 1
                    elif sig.disposition == ImportDisposition.DUPLICATE_SKIPPED:
                        duplicates += 1
                    else:
                        errors += 1

                    issue_code_counts[sig.code] = (
                        issue_code_counts.get(sig.code, 0) + 1
                    )
                    batch_repo.add_issue(
                        ImportIssue(
                            batch_id=batch.id,
                            source_record_id=display_id[:80],
                            record_type=rec_type[:40],
                            disposition=sig.disposition.value,
                            issue_code=sig.code[:60],
                            message=sig.message[:400],
                            raw_payload_json=json.dumps(
                                rec, default=str, ensure_ascii=False
                            ),
                        )
                    )
                except Exception as exc:
                    errors += 1
                    code = "UNEXPECTED_IMPORT_ERROR"
                    issue_code_counts[code] = issue_code_counts.get(code, 0) + 1
                    batch_repo.add_issue(
                        ImportIssue(
                            batch_id=batch.id,
                            source_record_id=display_id[:80],
                            record_type=rec_type[:40],
                            disposition=ImportDisposition.ERROR.value,
                            issue_code=code,
                            message=f"Unexpected import failure: {type(exc).__name__}: {exc}"[
                                :400
                            ],
                            raw_payload_json=json.dumps(
                                rec, default=str, ensure_ascii=False
                            ),
                        )
                    )

            batch.accepted_count = accepted
            batch.rejected_count = rejected
            batch.quarantined_count = quarantined
            batch.duplicate_skipped_count = duplicates
            batch.error_count = errors
            batch.completed_at = utc_now()
            batch.summary_json = json.dumps(issue_code_counts, sort_keys=True)

            AuditRepository(session).record(
                event_type=AuditEventType.HISTORICAL_IMPORT_COMPLETED,
                entity_type="IMPORT_BATCH",
                entity_id=batch.batch_key,
                actor_user_id=actor_user_id,
                summary=(
                    f"Completed historical import {batch.batch_key}: "
                    f"accepted={accepted}, rejected={rejected}, quarantined={quarantined}, "
                    f"duplicates={duplicates}, errors={errors}."
                ),
                details={
                    "source_filename": source_filename,
                    "total_records": len(records),
                    "accepted": accepted,
                    "rejected": rejected,
                    "quarantined": quarantined,
                    "duplicate_skipped": duplicates,
                    "errors": errors,
                },
            )

            return ImportValidationReport(
                batch_id=batch.id,
                batch_key=batch.batch_key,
                source_filename=source_filename,
                total_records=len(records),
                accepted_count=accepted,
                rejected_count=rejected,
                quarantined_count=quarantined,
                duplicate_skipped_count=duplicates,
                error_count=errors,
                issue_counts_by_code=issue_code_counts,
            )

    def _import_book_record(
        self, session: Session, source_id: str, rec: dict[str, Any]
    ) -> None:
        title = str(rec.get("title") or "").strip()
        publisher = str(rec.get("publisher") or "").strip()
        category = str(rec.get("category") or "").strip()
        language = str(rec.get("language") or "English").strip() or "English"
        authors = rec.get("authors")
        raw_year = rec.get("publication_year")

        if not title or not publisher or not category or not authors or raw_year is None:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "MISSING_REQUIRED_FIELD",
                "Historical BOOK record requires title, publisher, publication_year, category, and authors.",
            )

        try:
            pub_year = int(raw_year)
        except (TypeError, ValueError) as exc:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "INVALID_PUBLICATION_YEAR",
                f"Invalid publication_year '{raw_year}'.",
            ) from exc

        if pub_year < 1450 or pub_year > 2100:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "INVALID_PUBLICATION_YEAR",
                f"Publication year {pub_year} outside valid range 1450-2100.",
            )

        raw_isbn = rec.get("isbn")
        try:
            parsed_isbn = ISBN.parse(str(raw_isbn) if raw_isbn is not None else None)
        except InvalidISBNError as exc:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "MALFORMED_ISBN",
                str(exc),
            ) from exc

        book_repo = BookRepository(session)
        if parsed_isbn is not None:
            if book_repo.get_by_isbn(parsed_isbn.normalized) is not None:
                raise _ImportDispositionSignal(
                    ImportDisposition.DUPLICATE_SKIPPED,
                    "DUPLICATE_CATALOG_ISBN",
                    f"Catalog already contains ISBN {parsed_isbn.normalized}.",
                )
        else:
            edition = str(rec.get("edition") or "").strip() or None
            if (
                book_repo.find_exact_non_isbn_match(
                    title=title,
                    publisher=publisher,
                    publication_year=pub_year,
                    edition=edition,
                )
                is not None
            ):
                raise _ImportDispositionSignal(
                    ImportDisposition.DUPLICATE_SKIPPED,
                    "DUPLICATE_CATALOG_NON_ISBN",
                    f"Exact non-ISBN catalog match already exists for '{title}'.",
                )
            # Check if a book with the exact same title exists without ISBN -> quarantine for librarian review
            same_title = session.scalar(
                select(Book).where(Book.title.ilike(title))
            )
            if same_title is not None:
                raise _ImportDispositionSignal(
                    ImportDisposition.QUARANTINED,
                    "AMBIGUOUS_CATALOG_RECORD_NO_ISBN",
                    f"Missing ISBN and similar catalog title '{same_title.title}' exists; quarantined for librarian review instead of auto-merging.",
                )

        author_names = (
            [str(a).strip() for a in authors if str(a).strip()]
            if isinstance(authors, list)
            else [a.strip() for a in str(authors).split(",") if a.strip()]
        )
        if not author_names:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "MISSING_REQUIRED_FIELD",
                "Book record has no valid author names.",
            )

        author_repo = AuthorRepository(session)
        author_entities: list[Author] = [
            author_repo.get_or_create(name)[0] for name in author_names
        ]
        new_book = Book(
            title=title,
            isbn=parsed_isbn.normalized if parsed_isbn else None,
            publisher=publisher,
            publication_year=pub_year,
            language=language,
            category=category,
            edition=str(rec.get("edition") or "").strip() or None,
            description=str(rec.get("description") or "").strip() or None,
        )
        new_book.authors = author_entities
        book_repo.add(new_book)

    def _import_loan_record(
        self, session: Session, source_id: str, rec: dict[str, Any], calendar
    ) -> None:
        loan_repo = LoanRepository(session)
        if loan_repo.get_by_source_reference(source_id) is not None:
            raise _ImportDispositionSignal(
                ImportDisposition.DUPLICATE_SKIPPED,
                "ALREADY_IMPORTED_SOURCE_REF",
                f"Historical loan '{source_id}' was already imported in a previous run.",
            )

        member_code = str(rec.get("member_code") or "").strip()
        copy_accession = str(rec.get("copy_accession") or "").strip()
        branch_code = str(rec.get("branch_code") or "").strip()
        raw_status = str(rec.get("status") or "").strip().upper()

        if not member_code or not copy_accession or not branch_code or not raw_status:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "MISSING_REQUIRED_FIELD",
                "Historical LOAN requires member_code, copy_accession, branch_code, and status.",
            )

        if raw_status not in (LoanStatus.RETURNED.value, LoanStatus.ACTIVE.value):
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "INVALID_LOAN_STATUS",
                f"Invalid historical loan status '{raw_status}'.",
            )

        member: Member | None = MemberRepository(session).get_by_member_code(member_code)
        if member is None:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "INVALID_MEMBER_REFERENCE",
                f"Referenced member_code '{member_code}' does not exist.",
            )

        branch: LibraryBranch | None = BranchRepository(session).get_by_code(branch_code)
        if branch is None:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "INVALID_BRANCH_REFERENCE",
                f"Referenced branch_code '{branch_code}' does not exist.",
            )

        copy: BookCopy | None = CopyRepository(session).get_by_accession(copy_accession)
        if copy is None:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "INVALID_COPY_REFERENCE",
                f"Referenced copy_accession '{copy_accession}' does not exist.",
            )

        checkout_date = self._parse_iso_date(rec.get("checkout_date"), "checkout_date")
        due_date = self._parse_iso_date(rec.get("due_date"), "due_date")

        if due_date < checkout_date:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "DUE_BEFORE_CHECKOUT",
                f"Invalid dates: due_date ({due_date}) precedes checkout_date ({checkout_date}).",
            )

        raw_renewals = rec.get("renewal_count", 0)
        try:
            renewal_count = int(raw_renewals)
        except (TypeError, ValueError) as exc:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "INVALID_RENEWAL_COUNT",
                f"Non-integer renewal_count '{raw_renewals}'.",
            ) from exc

        if renewal_count < 0:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "NEGATIVE_RENEWAL_COUNT",
                f"Negative renewal_count ({renewal_count}) is invalid.",
            )
        if renewal_count > member.tier.max_renewals:
            raise _ImportDispositionSignal(
                ImportDisposition.QUARANTINED,
                "EXCESSIVE_RENEWAL_COUNT",
                f"Historical renewal_count ({renewal_count}) exceeds {member.tier.code} max_renewals ({member.tier.max_renewals}); requires librarian review.",
            )

        return_date: date | None = None
        returned_utc: datetime | None = None
        if raw_status == LoanStatus.RETURNED.value:
            raw_ret = rec.get("return_date")
            if not raw_ret or not str(raw_ret).strip():
                raise _ImportDispositionSignal(
                    ImportDisposition.QUARANTINED,
                    "MISSING_RETURN_DATE",
                    "Loan marked RETURNED has missing return_date; quarantined rather than inventing a return date.",
                )
            return_date = self._parse_iso_date(raw_ret, "return_date")
            if return_date < checkout_date:
                raise _ImportDispositionSignal(
                    ImportDisposition.REJECTED,
                    "RETURN_BEFORE_CHECKOUT",
                    f"Invalid dates: return_date ({return_date}) precedes checkout_date ({checkout_date}).",
                )
            returned_utc = self._local_day_noon_utc(return_date)
        else:
            # Active historical loan: ensure copy has no active loan and is AVAILABLE
            if (
                copy.status_enum != CopyStatus.AVAILABLE
                or loan_repo.get_active_loan_for_copy(copy.id) is not None
            ):
                raise _ImportDispositionSignal(
                    ImportDisposition.REJECTED,
                    "CONFLICTING_ACTIVE_LOAN",
                    f"Copy '{copy.accession_number}' already has an active loan or non-available status ({copy.status}).",
                )

        # Check overlapping historical loan intervals on the same copy
        checkout_utc = self._local_day_noon_utc(checkout_date)
        existing_copy_loans = session.scalars(
            select(Loan).where(Loan.copy_id == copy.id)
        ).all()
        cand_end = returned_utc or datetime.max.replace(tzinfo=timezone.utc)
        for prior in existing_copy_loans:
            prior_start = prior.checked_out_at
            prior_end = prior.returned_at or datetime.max.replace(tzinfo=timezone.utc)
            if checkout_utc < prior_end and cand_end > prior_start:
                raise _ImportDispositionSignal(
                    ImportDisposition.REJECTED,
                    "OVERLAPPING_LOAN_HISTORY",
                    f"Loan interval overlaps existing loan {prior.loan_number} on copy {copy.accession_number}.",
                )

        # Validate optional fine payload BEFORE writing loan
        fine_payload = rec.get("fine")
        validated_fine_data: tuple[Decimal, Decimal, int, Decimal] | None = None
        if fine_payload is not None:
            if not isinstance(fine_payload, dict):
                raise _ImportDispositionSignal(
                    ImportDisposition.REJECTED,
                    "INVALID_FINE_STRUCTURE",
                    "Historical fine field must be an object.",
                )
            try:
                assessed_amt = quantize_money(fine_payload.get("assessed_amount", "0"))
                paid_amt = quantize_money(fine_payload.get("paid_amount", "0"))
            except (TypeError, ValueError) as exc:
                raise _ImportDispositionSignal(
                    ImportDisposition.REJECTED,
                    "INVALID_FINE_AMOUNT",
                    f"Unparseable fine monetary value: {exc}",
                ) from exc

            if assessed_amt < ZERO_MONEY or paid_amt < ZERO_MONEY:
                raise _ImportDispositionSignal(
                    ImportDisposition.REJECTED,
                    "NEGATIVE_FINE_AMOUNT",
                    f"Negative fine or payment amount (assessed={assessed_amt}, paid={paid_amt}).",
                )
            if paid_amt > assessed_amt:
                raise _ImportDispositionSignal(
                    ImportDisposition.REJECTED,
                    "OVERPAYMENT_EXCEEDS_ASSESSED_FINE",
                    f"Historical paid_amount ({paid_amt}) exceeds assessed_amount ({assessed_amt}).",
                )
            if raw_status != LoanStatus.RETURNED.value or return_date is None:
                raise _ImportDispositionSignal(
                    ImportDisposition.QUARANTINED,
                    "FINE_ON_UNRETURNED_LOAN",
                    "Historical finalized fine attached to an unreturned loan; quarantined for review.",
                )

            # Compare against policy calculation if no explicit legacy_rate_verified flag
            expected_bd = PolicyFactory.create_fine_policy(
                member.tier, calendar
            ).calculate_fine(
                due_date=due_date,
                effective_return_date=return_date,
                checkout_date=checkout_date,
                branch_id=branch.id,
            )
            if (
                assessed_amt != expected_bd.assessed_amount
                and not fine_payload.get("legacy_rate_verified", False)
            ):
                raise _ImportDispositionSignal(
                    ImportDisposition.QUARANTINED,
                    "UNEXPLAINED_FINE_DISCREPANCY",
                    f"Source fine INR {assessed_amt} differs from policy INR {expected_bd.assessed_amount} without legacy_rate_verified.",
                )
            if assessed_amt > ZERO_MONEY:
                validated_fine_data = (
                    assessed_amt,
                    paid_amt,
                    expected_bd.eligible_overdue_days,
                    expected_bd.daily_rate,
                )

        loan = Loan(
            loan_number=loan_repo.next_loan_number(checkout_date),
            member_id=member.id,
            copy_id=copy.id,
            issuing_branch_id=branch.id,
            returned_branch_id=branch.id if returned_utc else None,
            checked_out_at=checkout_utc,
            due_date=due_date,
            returned_at=returned_utc,
            status=raw_status,
            renewal_count=renewal_count,
            return_notes=str(rec.get("notes") or "").strip() or None,
            source_reference=source_id,
            created_at=checkout_utc,
            updated_at=returned_utc or checkout_utc,
        )
        loan_repo.add(loan)

        if raw_status == LoanStatus.ACTIVE.value:
            copy.status = CopyStatus.ON_LOAN.value

        if validated_fine_data is not None and returned_utc is not None and return_date is not None:
            assessed_amt, paid_amt, elig_days, rate_applied = validated_fine_data
            fine_repo = FineRepository(session)
            if paid_amt == ZERO_MONEY:
                f_status = FineStatus.UNPAID.value
            elif paid_amt == assessed_amt:
                f_status = FineStatus.PAID.value
            else:
                f_status = FineStatus.PARTIALLY_PAID.value

            fine = Fine(
                fine_number=fine_repo.next_fine_number(return_date),
                loan_id=loan.id,
                member_id=member.id,
                assessed_amount=assessed_amt,
                paid_amount=paid_amt,
                waived_amount=ZERO_MONEY,
                eligible_overdue_days=elig_days,
                daily_rate_applied=rate_applied,
                status=f_status,
                reason="Historical overdue return fine",
                assessed_at=returned_utc,
                source_reference=f"{source_id}:FINE",
                created_at=returned_utc,
                updated_at=returned_utc,
            )
            fine_repo.add_fine(fine)

            if paid_amt > ZERO_MONEY:
                method_raw = str(fine_payload.get("payment_method") or "CASH").upper()
                method = (
                    method_raw
                    if method_raw in {m.value for m in PaymentMethod}
                    else PaymentMethod.CASH.value
                )
                payment = FinePayment(
                    receipt_number=fine_repo.next_receipt_number(return_date),
                    fine_id=fine.id,
                    member_id=member.id,
                    branch_id=branch.id,
                    amount=paid_amt,
                    payment_method=method,
                    idempotency_key=f"{source_id}:PMT",
                    reference_note="Imported historical fine payment",
                    paid_at=returned_utc,
                    created_at=returned_utc,
                )
                fine_repo.add_payment(payment)

    def _import_reservation_record(
        self, session: Session, source_id: str, rec: dict[str, Any]
    ) -> None:
        res_repo = ReservationRepository(session)
        if res_repo.get_by_source_reference(source_id) is not None:
            raise _ImportDispositionSignal(
                ImportDisposition.DUPLICATE_SKIPPED,
                "ALREADY_IMPORTED_SOURCE_REF",
                f"Historical reservation '{source_id}' was already imported.",
            )

        member_code = str(rec.get("member_code") or "").strip()
        book_isbn = str(rec.get("book_isbn") or "").strip()
        raw_status = str(rec.get("status") or "").strip().upper()

        if not member_code or not book_isbn or not raw_status:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "MISSING_REQUIRED_FIELD",
                "Historical RESERVATION requires member_code, book_isbn, and status.",
            )

        if raw_status not in {s.value for s in ReservationStatus}:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "INVALID_RESERVATION_STATUS",
                f"Invalid reservation status '{raw_status}'.",
            )

        member = MemberRepository(session).get_by_member_code(member_code)
        if member is None:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "INVALID_MEMBER_REFERENCE",
                f"Referenced member_code '{member_code}' does not exist.",
            )

        book = BookRepository(session).get_by_isbn(
            book_isbn.replace("-", "").replace(" ", "").upper()
        )
        if book is None:
            raise _ImportDispositionSignal(
                ImportDisposition.REJECTED,
                "INVALID_BOOK_REFERENCE",
                f"Referenced book_isbn '{book_isbn}' does not exist.",
            )

        branch_id: int | None = None
        if rec.get("branch_code"):
            br = BranchRepository(session).get_by_code(str(rec["branch_code"]))
            if br is None:
                raise _ImportDispositionSignal(
                    ImportDisposition.REJECTED,
                    "INVALID_BRANCH_REFERENCE",
                    f"Referenced branch_code '{rec['branch_code']}' does not exist.",
                )
            branch_id = br.id

        created_day = self._parse_iso_date(rec.get("created_date"), "created_date")
        created_utc = self._local_day_noon_utc(created_day)

        if raw_status in (
            ReservationStatus.WAITING.value,
            ReservationStatus.READY_FOR_PICKUP.value,
        ):
            if res_repo.get_active_for_member_and_book(member.id, book.id) is not None:
                raise _ImportDispositionSignal(
                    ImportDisposition.REJECTED,
                    "CONFLICTING_RESERVATION",
                    f"Member {member.member_code} already has an active reservation for '{book.title}'.",
                )

        allocated_copy_id: int | None = None
        pickup_deadline_utc: datetime | None = None
        if raw_status == ReservationStatus.READY_FOR_PICKUP.value:
            acc = str(rec.get("allocated_copy_accession") or "").strip()
            if not acc:
                raise _ImportDispositionSignal(
                    ImportDisposition.REJECTED,
                    "MISSING_ALLOCATED_COPY",
                    "READY_FOR_PICKUP reservation is missing allocated_copy_accession.",
                )
            copy = CopyRepository(session).get_by_accession(acc)
            if copy is None or copy.book_id != book.id:
                raise _ImportDispositionSignal(
                    ImportDisposition.REJECTED,
                    "INVALID_COPY_REFERENCE",
                    f"Invalid allocated copy '{acc}' for book '{book.title}'.",
                )
            if (
                copy.status_enum != CopyStatus.AVAILABLE
                or res_repo.get_ready_for_copy(copy.id) is not None
            ):
                raise _ImportDispositionSignal(
                    ImportDisposition.REJECTED,
                    "CONFLICTING_RESERVATION",
                    f"Copy '{acc}' is not available for READY_FOR_PICKUP hold allocation.",
                )
            deadline_day = self._parse_iso_date(
                rec.get("pickup_deadline_date"), "pickup_deadline_date"
            )
            if deadline_day < created_day:
                raise _ImportDispositionSignal(
                    ImportDisposition.REJECTED,
                    "INVALID_PICKUP_DEADLINE",
                    "pickup_deadline_date precedes created_date.",
                )
            allocated_copy_id = copy.id
            pickup_deadline_utc = self._local_day_noon_utc(deadline_day)
            copy.status = CopyStatus.RESERVED.value

        reservation = Reservation(
            reservation_number=res_repo.next_reservation_number(created_day),
            book_id=book.id,
            member_id=member.id,
            preferred_branch_id=branch_id,
            allocated_copy_id=allocated_copy_id,
            status=raw_status,
            allocated_at=created_utc if allocated_copy_id else None,
            pickup_deadline_at=pickup_deadline_utc,
            fulfilled_at=created_utc
            if raw_status == ReservationStatus.FULFILLED.value
            else None,
            cancelled_at=created_utc
            if raw_status == ReservationStatus.CANCELLED.value
            else None,
            expired_at=created_utc
            if raw_status == ReservationStatus.EXPIRED.value
            else None,
            source_reference=source_id,
            created_at=created_utc,
            updated_at=created_utc,
        )
        res_repo.add(reservation)

    def search_import_issues(
        self,
        *,
        batch_id: int | None = None,
        disposition: str | None = None,
        record_type: str | None = None,
        issue_code: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> Page[ImportIssue]:
        with read_session(self.session_factory) as session:
            return ImportBatchRepository(session).search_issues(
                batch_id=batch_id,
                disposition=disposition,
                record_type=record_type,
                issue_code=issue_code,
                page=page,
                per_page=per_page,
            )

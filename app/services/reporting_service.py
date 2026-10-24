"""Reporting and Dashboard analytics service backed by live database queries."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import io
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, sessionmaker

from app.domain.enums import (
    CopyStatus,
    FineStatus,
    LoanStatus,
    MembershipStatus,
    ReservationStatus,
)
from app.domain.value_objects import (
    ZERO_MONEY,
    local_today,
    quantize_money,
    to_local_date,
)
from app.infrastructure.transactions import read_session
from app.models import (
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
    FineRepository,
    HolidayRepository,
    ImportBatchRepository,
    LoanRepository,
    Page,
    paginate_statement,
)
from app.services.fine_service import FineService


def sanitize_csv_value(value: Any) -> str:
    """Escape values against spreadsheet formula injection (OWASP CSV Injection mitigation)."""
    if value is None:
        return ""
    text_val = str(value)
    if not text_val:
        return ""
    if text_val[0] in ("=", "+", "-", "@", "\t", "\r"):
        return f"'{text_val}"
    stripped = text_val.lstrip()
    if stripped and stripped[0] in ("=", "+", "-", "@"):
        return f"'{text_val}"
    return text_val


def rows_to_safe_csv(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Render headers and rows into a UTF-8 CSV string with formula-injection protection."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([sanitize_csv_value(h) for h in headers])
    for row in rows:
        writer.writerow([sanitize_csv_value(cell) for cell in row])
    return buf.getvalue()


@dataclass(frozen=True, slots=True)
class BranchSummaryRow:
    branch_id: int
    branch_code: str
    branch_name: str
    is_active: bool
    total_copies: int
    available_copies: int
    on_loan_copies: int
    reserved_copies: int
    active_loans: int
    overdue_loans: int
    ready_pickups: int


@dataclass(frozen=True, slots=True)
class DashboardStats:
    total_titles: int
    total_copies: int
    available_copies: int
    copies_on_loan: int
    active_loans: int
    overdue_loans: int
    registered_members: int
    active_members: int
    suspended_members: int
    pending_reservations: int
    ready_for_pickup_reservations: int
    total_outstanding_fines: Decimal
    fine_payments_collected: Decimal
    branch_summaries: list[BranchSummaryRow]
    recent_loans: Sequence[Loan]


@dataclass(frozen=True, slots=True)
class OverdueReportRow:
    loan_id: int
    loan_number: str
    branch_id: int
    branch_code: str
    branch_name: str
    book_id: int
    book_title: str
    copy_accession: str
    member_id: int
    member_code: str
    member_name: str
    checkout_date: date
    due_date: date
    returned_date: date | None
    loan_status: str
    days_overdue: int
    eligible_overdue_days: int
    estimated_fine: Decimal
    loan_outstanding_fine: Decimal
    member_total_outstanding_fine: Decimal


@dataclass(frozen=True, slots=True)
class InventoryBranchRow:
    branch_code: str
    branch_name: str
    book_id: int
    book_title: str
    isbn: str | None
    category: str
    total_copies: int
    available_copies: int
    on_loan_copies: int
    reserved_copies: int


class ReportingService:
    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        fine_service: FineService | None = None,
        library_timezone: str = "Asia/Kolkata",
    ) -> None:
        self.session_factory = session_factory
        self.library_timezone = library_timezone
        self.fine_service = fine_service or FineService(
            session_factory, library_timezone=library_timezone
        )

    def get_dashboard_stats(self, *, as_of_date: date | None = None) -> DashboardStats:
        today = as_of_date or local_today(self.library_timezone)
        with read_session(self.session_factory) as session:
            total_titles = int(session.scalar(select(func.count(Book.id))) or 0)
            total_copies = int(session.scalar(select(func.count(BookCopy.id))) or 0)
            available_copies = int(
                session.scalar(
                    select(func.count(BookCopy.id)).where(
                        BookCopy.status == CopyStatus.AVAILABLE.value
                    )
                )
                or 0
            )
            copies_on_loan = int(
                session.scalar(
                    select(func.count(BookCopy.id)).where(
                        BookCopy.status == CopyStatus.ON_LOAN.value
                    )
                )
                or 0
            )
            active_loans = int(
                session.scalar(
                    select(func.count(Loan.id)).where(
                        Loan.status == LoanStatus.ACTIVE.value
                    )
                )
                or 0
            )
            overdue_loans = int(
                session.scalar(
                    select(func.count(Loan.id)).where(
                        Loan.status == LoanStatus.ACTIVE.value,
                        Loan.due_date < today,
                    )
                )
                or 0
            )
            registered_members = int(
                session.scalar(select(func.count(Member.id))) or 0
            )
            active_members = int(
                session.scalar(
                    select(func.count(Member.id)).where(
                        Member.status == MembershipStatus.ACTIVE.value
                    )
                )
                or 0
            )
            suspended_members = int(
                session.scalar(
                    select(func.count(Member.id)).where(
                        Member.status == MembershipStatus.SUSPENDED.value
                    )
                )
                or 0
            )
            pending_reservations = int(
                session.scalar(
                    select(func.count(Reservation.id)).where(
                        Reservation.status == ReservationStatus.WAITING.value
                    )
                )
                or 0
            )
            ready_reservations = int(
                session.scalar(
                    select(func.count(Reservation.id)).where(
                        Reservation.status == ReservationStatus.READY_FOR_PICKUP.value
                    )
                )
                or 0
            )

            open_fines = session.scalars(
                select(Fine).where(
                    Fine.status.in_(
                        [FineStatus.UNPAID.value, FineStatus.PARTIALLY_PAID.value]
                    )
                )
            ).all()
            total_outstanding = quantize_money(
                sum((f.outstanding_balance for f in open_fines), ZERO_MONEY)
            )

            payments = session.scalars(select(FinePayment)).all()
            total_collected = quantize_money(
                sum((quantize_money(p.amount) for p in payments), ZERO_MONEY)
            )

            branches = session.scalars(
                select(LibraryBranch).order_by(LibraryBranch.id.asc())
            ).all()
            branch_summaries: list[BranchSummaryRow] = []
            for br in branches:
                br_total = int(
                    session.scalar(
                        select(func.count(BookCopy.id)).where(
                            BookCopy.owning_branch_id == br.id
                        )
                    )
                    or 0
                )
                br_avail = int(
                    session.scalar(
                        select(func.count(BookCopy.id)).where(
                            BookCopy.owning_branch_id == br.id,
                            BookCopy.status == CopyStatus.AVAILABLE.value,
                        )
                    )
                    or 0
                )
                br_loan_copies = int(
                    session.scalar(
                        select(func.count(BookCopy.id)).where(
                            BookCopy.owning_branch_id == br.id,
                            BookCopy.status == CopyStatus.ON_LOAN.value,
                        )
                    )
                    or 0
                )
                br_res_copies = int(
                    session.scalar(
                        select(func.count(BookCopy.id)).where(
                            BookCopy.owning_branch_id == br.id,
                            BookCopy.status == CopyStatus.RESERVED.value,
                        )
                    )
                    or 0
                )
                br_active_loans = int(
                    session.scalar(
                        select(func.count(Loan.id)).where(
                            Loan.issuing_branch_id == br.id,
                            Loan.status == LoanStatus.ACTIVE.value,
                        )
                    )
                    or 0
                )
                br_overdue = int(
                    session.scalar(
                        select(func.count(Loan.id)).where(
                            Loan.issuing_branch_id == br.id,
                            Loan.status == LoanStatus.ACTIVE.value,
                            Loan.due_date < today,
                        )
                    )
                    or 0
                )
                br_ready = int(
                    session.scalar(
                        select(func.count(Reservation.id))
                        .join(
                            BookCopy, BookCopy.id == Reservation.allocated_copy_id
                        )
                        .where(
                            Reservation.status
                            == ReservationStatus.READY_FOR_PICKUP.value,
                            BookCopy.owning_branch_id == br.id,
                        )
                    )
                    or 0
                )
                branch_summaries.append(
                    BranchSummaryRow(
                        branch_id=br.id,
                        branch_code=br.code,
                        branch_name=br.name,
                        is_active=br.is_active,
                        total_copies=br_total,
                        available_copies=br_avail,
                        on_loan_copies=br_loan_copies,
                        reserved_copies=br_res_copies,
                        active_loans=br_active_loans,
                        overdue_loans=br_overdue,
                        ready_pickups=br_ready,
                    )
                )

            recent_loans = LoanRepository(session).search(page=1, per_page=8).items
            return DashboardStats(
                total_titles=total_titles,
                total_copies=total_copies,
                available_copies=available_copies,
                copies_on_loan=copies_on_loan,
                active_loans=active_loans,
                overdue_loans=overdue_loans,
                registered_members=registered_members,
                active_members=active_members,
                suspended_members=suspended_members,
                pending_reservations=pending_reservations,
                ready_for_pickup_reservations=ready_reservations,
                total_outstanding_fines=total_outstanding,
                fine_payments_collected=total_collected,
                branch_summaries=branch_summaries,
                recent_loans=recent_loans,
            )

    def get_overdue_report(
        self,
        *,
        branch_id: int | None = None,
        mode: str = "active",
        as_of_date: date | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> Page[OverdueReportRow]:
        """Generate overdue loan report per branch without join row multiplication.

        `mode='active'`: currently unreturned loans past due_date.
        `mode='historical'`: returned loans whose return date exceeded due_date.
        """
        today = as_of_date or local_today(self.library_timezone)
        clean_mode = mode.strip().lower() if mode else "active"

        with read_session(self.session_factory) as session:
            stmt = select(Loan).options(
                joinedload(Loan.member).joinedload(Member.tier),
                joinedload(Loan.copy).joinedload(BookCopy.book),
                joinedload(Loan.issuing_branch),
                joinedload(Loan.fine),
            )
            if branch_id is not None:
                stmt = stmt.where(Loan.issuing_branch_id == branch_id)

            if clean_mode == "historical":
                stmt = stmt.where(
                    Loan.status == LoanStatus.RETURNED.value,
                    Loan.returned_at.is_not(None),
                ).order_by(Loan.due_date.desc(), Loan.id.desc())
                # Filter returned_local_date > due_date accurately in Python for timezone precision,
                # pre-filtering in SQL via date(returned_at) >= due_date
                all_returned = list(session.scalars(stmt).unique().all())
                overdue_loans = [
                    ln
                    for ln in all_returned
                    if ln.returned_at is not None
                    and to_local_date(ln.returned_at, self.library_timezone)
                    > ln.due_date
                ]
                total = len(overdue_loans)
                clean_per_page = min(200, max(1, int(per_page)))
                total_pages = max(1, (total + clean_per_page - 1) // clean_per_page)
                clean_page = min(max(1, int(page)), total_pages)
                start = (clean_page - 1) * clean_per_page
                loan_slice = overdue_loans[start : start + clean_per_page]
            else:
                stmt = stmt.where(
                    Loan.status == LoanStatus.ACTIVE.value,
                    Loan.due_date < today,
                ).order_by(Loan.due_date.asc(), Loan.id.asc())
                paged_loans = paginate_statement(
                    session, stmt, page=page, per_page=per_page
                )
                total = paged_loans.total
                clean_page = paged_loans.page
                clean_per_page = paged_loans.per_page
                loan_slice = list(paged_loans.items)

            calendar = HolidayRepository(session).build_calendar()
            fine_repo = FineRepository(session)
            member_balance_cache: dict[int, Decimal] = {}
            rows: list[OverdueReportRow] = []

            for ln in loan_slice:
                eval_date = (
                    to_local_date(ln.returned_at, self.library_timezone)
                    if ln.returned_at is not None
                    else today
                )
                breakdown = self.fine_service.calculate_loan_fine_in_session(
                    session,
                    ln,
                    effective_return_date=eval_date,
                    calendar=calendar,
                )
                loan_outstanding = (
                    ln.fine.outstanding_balance
                    if ln.fine is not None
                    else breakdown.assessed_amount
                )
                if ln.member_id not in member_balance_cache:
                    member_balance_cache[
                        ln.member_id
                    ] = fine_repo.get_member_outstanding_balance(ln.member_id)

                rows.append(
                    OverdueReportRow(
                        loan_id=ln.id,
                        loan_number=ln.loan_number,
                        branch_id=ln.issuing_branch.id,
                        branch_code=ln.issuing_branch.code,
                        branch_name=ln.issuing_branch.name,
                        book_id=ln.copy.book.id,
                        book_title=ln.copy.book.title,
                        copy_accession=ln.copy.accession_number,
                        member_id=ln.member.id,
                        member_code=ln.member.member_code,
                        member_name=ln.member.full_name,
                        checkout_date=to_local_date(
                            ln.checked_out_at, self.library_timezone
                        ),
                        due_date=ln.due_date,
                        returned_date=to_local_date(
                            ln.returned_at, self.library_timezone
                        )
                        if ln.returned_at
                        else None,
                        loan_status=ln.status,
                        days_overdue=breakdown.calendar_days_late,
                        eligible_overdue_days=breakdown.eligible_overdue_days,
                        estimated_fine=ln.fine.assessed_amount
                        if ln.fine is not None
                        else breakdown.assessed_amount,
                        loan_outstanding_fine=loan_outstanding,
                        member_total_outstanding_fine=member_balance_cache[
                            ln.member_id
                        ],
                    )
                )

            return Page(
                items=rows,
                total=total,
                page=clean_page,
                per_page=clean_per_page,
            )

    def get_available_inventory_report(
        self,
        *,
        branch_id: int | None = None,
        category: str | None = None,
        available_only: bool = False,
        page: int = 1,
        per_page: int = 25,
    ) -> Page[InventoryBranchRow]:
        with read_session(self.session_factory) as session:
            stmt = (
                select(
                    LibraryBranch.code.label("branch_code"),
                    LibraryBranch.name.label("branch_name"),
                    Book.id.label("book_id"),
                    Book.title.label("book_title"),
                    Book.isbn.label("isbn"),
                    Book.category.label("category"),
                    func.count(BookCopy.id).label("total_copies"),
                    func.sum(
                        func.iif(
                            BookCopy.status == CopyStatus.AVAILABLE.value, 1, 0
                        )
                    ).label("available_copies"),
                    func.sum(
                        func.iif(BookCopy.status == CopyStatus.ON_LOAN.value, 1, 0)
                    ).label("on_loan_copies"),
                    func.sum(
                        func.iif(BookCopy.status == CopyStatus.RESERVED.value, 1, 0)
                    ).label("reserved_copies"),
                )
                .join(BookCopy, BookCopy.owning_branch_id == LibraryBranch.id)
                .join(Book, Book.id == BookCopy.book_id)
                .where(BookCopy.status != CopyStatus.WITHDRAWN.value)
                .group_by(
                    LibraryBranch.id,
                    LibraryBranch.code,
                    LibraryBranch.name,
                    Book.id,
                    Book.title,
                    Book.isbn,
                    Book.category,
                )
                .order_by(LibraryBranch.code.asc(), Book.title.asc())
            )
            if branch_id is not None:
                stmt = stmt.where(LibraryBranch.id == branch_id)
            if category and category.strip():
                stmt = stmt.where(Book.category == category.strip())
            if available_only:
                stmt = stmt.having(
                    func.sum(
                        func.iif(
                            BookCopy.status == CopyStatus.AVAILABLE.value, 1, 0
                        )
                    )
                    > 0
                )

            count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
            total = int(session.scalar(count_stmt) or 0)
            clean_per_page = min(200, max(1, int(per_page)))
            total_pages = max(1, (total + clean_per_page - 1) // clean_per_page)
            clean_page = min(max(1, int(page)), total_pages)
            offset = (clean_page - 1) * clean_per_page
            raw_rows = session.execute(stmt.offset(offset).limit(clean_per_page)).all()

            items = [
                InventoryBranchRow(
                    branch_code=r.branch_code,
                    branch_name=r.branch_name,
                    book_id=r.book_id,
                    book_title=r.book_title,
                    isbn=r.isbn,
                    category=r.category,
                    total_copies=int(r.total_copies or 0),
                    available_copies=int(r.available_copies or 0),
                    on_loan_copies=int(r.on_loan_copies or 0),
                    reserved_copies=int(r.reserved_copies or 0),
                )
                for r in raw_rows
            ]
            return Page(
                items=items,
                total=total,
                page=clean_page,
                per_page=clean_per_page,
            )

    def get_data_quality_summary(
        self, *, batch_id: int | None = None
    ) -> dict[str, Any]:
        with read_session(self.session_factory) as session:
            batch_repo = ImportBatchRepository(session)
            batches = list(batch_repo.list_batches(limit=15))
            latest = batches[0] if batches else None
            selected_batch = (
                next((b for b in batches if b.id == batch_id), None)
                if batch_id is not None
                else None
            )
            if selected_batch is None and batch_id is not None:
                selected_batch = session.get(ImportBatch, batch_id)
            target_batch = selected_batch or latest

            breakdown_by_code: list[tuple[str, str, int]] = []
            if target_batch is not None:
                code_rows = session.execute(
                    select(
                        ImportIssue.disposition,
                        ImportIssue.issue_code,
                        func.count(ImportIssue.id),
                    )
                    .where(ImportIssue.batch_id == target_batch.id)
                    .group_by(ImportIssue.disposition, ImportIssue.issue_code)
                    .order_by(
                        ImportIssue.disposition.asc(),
                        func.count(ImportIssue.id).desc(),
                    )
                ).all()
                breakdown_by_code = [
                    (str(r[0]), str(r[1]), int(r[2])) for r in code_rows
                ]

            return {
                "latest_batch": latest,
                "selected_batch": target_batch,
                "batches": batches,
                "breakdown_by_code": breakdown_by_code,
            }

    def export_overdue_csv(
        self,
        *,
        branch_id: int | None = None,
        mode: str = "active",
        as_of_date: date | None = None,
    ) -> str:
        # Export up to 5000 matching rows safely
        all_rows: list[OverdueReportRow] = []
        current_page = 1
        while True:
            page_res = self.get_overdue_report(
                branch_id=branch_id,
                mode=mode,
                as_of_date=as_of_date,
                page=current_page,
                per_page=200,
            )
            all_rows.extend(page_res.items)
            if not page_res.has_next or len(all_rows) >= 5000:
                break
            current_page += 1

        headers = [
            "Branch Code",
            "Branch Name",
            "Loan Number",
            "Book Title",
            "Copy Accession",
            "Member ID",
            "Member Name",
            "Checkout Date",
            "Due Date",
            "Returned Date",
            "Loan Status",
            "Calendar Days Overdue",
            "Chargeable Overdue Days",
            "Estimated/Assessed Fine (INR)",
            "Loan Unpaid Balance (INR)",
            "Member Total Unpaid Fines (INR)",
        ]
        csv_rows = [
            [
                r.branch_code,
                r.branch_name,
                r.loan_number,
                r.book_title,
                r.copy_accession,
                r.member_code,
                r.member_name,
                r.checkout_date.isoformat(),
                r.due_date.isoformat(),
                r.returned_date.isoformat() if r.returned_date else "",
                r.loan_status,
                r.days_overdue,
                r.eligible_overdue_days,
                f"{r.estimated_fine:.2f}",
                f"{r.loan_outstanding_fine:.2f}",
                f"{r.member_total_outstanding_fine:.2f}",
            ]
            for r in all_rows
        ]
        return rows_to_safe_csv(headers, csv_rows)

"""Administration service for branches, membership policies, holidays, and corrections."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from app.domain.enums import AuditEventType, CopyStatus, LoanStatus
from app.domain.exceptions import (
    BranchNotFoundError,
    CopyNotFoundError,
    DuplicateRecordError,
    InvalidStateTransitionError,
    LoanNotFoundError,
)
from app.domain.value_objects import (
    ZERO_MONEY,
    normalize_email,
    normalize_phone,
    quantize_money,
    to_local_date,
    utc_now,
)
from app.infrastructure.transactions import read_session, write_transaction
from app.models import AuditEvent, BookCopy, LibraryBranch, LibraryHoliday, Loan, MembershipTier
from app.repositories import (
    AuditRepository,
    BranchRepository,
    CopyRepository,
    HolidayRepository,
    LoanRepository,
    Page,
    TierRepository,
)


class AdministrationService:
    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        library_timezone: str = "Asia/Kolkata",
    ) -> None:
        self.session_factory = session_factory
        self.library_timezone = library_timezone

    def list_branches(self, *, active_only: bool = False) -> list[LibraryBranch]:
        with read_session(self.session_factory) as session:
            return list(BranchRepository(session).list_all(active_only=active_only))

    def get_branch(self, branch_id: int) -> LibraryBranch:
        with read_session(self.session_factory) as session:
            branch = BranchRepository(session).get_by_id(branch_id)
            if branch is None:
                raise BranchNotFoundError(f"Branch #{branch_id} was not found.")
            return branch

    def create_branch(
        self,
        *,
        code: str,
        name: str,
        address: str,
        pincode: str,
        phone: str,
        email: str,
        city: str = "Lucknow",
        state: str = "Uttar Pradesh",
        actor_user_id: int | None = None,
    ) -> LibraryBranch:
        clean_code = code.strip().upper()
        clean_name = name.strip()
        if not clean_code or not clean_name or not address.strip():
            raise ValueError("Branch code, name, and address are required.")
        norm_email = normalize_email(email)
        norm_phone = normalize_phone(phone)
        if not norm_email or not norm_phone:
            raise ValueError("Valid branch email and phone are required.")

        with write_transaction(self.session_factory) as session:
            repo = BranchRepository(session)
            if repo.get_by_code(clean_code) is not None:
                raise DuplicateRecordError(f"Branch code '{clean_code}' already exists.")
            if repo.get_by_name(clean_name) is not None:
                raise DuplicateRecordError(f"Branch name '{clean_name}' already exists.")
            branch = LibraryBranch(
                code=clean_code,
                name=clean_name,
                address=address.strip(),
                city=city.strip() or "Lucknow",
                state=state.strip() or "Uttar Pradesh",
                pincode=pincode.strip(),
                phone=norm_phone,
                email=norm_email,
                is_active=True,
            )
            repo.add(branch)
            AuditRepository(session).record(
                event_type=AuditEventType.BRANCH_MANAGED,
                entity_type="BRANCH",
                entity_id=branch.id,
                actor_user_id=actor_user_id,
                branch_id=branch.id,
                summary=f"Created library branch '{branch.name}' ({branch.code}).",
            )
            return branch

    def update_branch(
        self,
        branch_id: int,
        *,
        name: str,
        address: str,
        pincode: str,
        phone: str,
        email: str,
        is_active: bool = True,
        actor_user_id: int | None = None,
    ) -> LibraryBranch:
        norm_email = normalize_email(email)
        norm_phone = normalize_phone(phone)
        if not norm_email or not norm_phone:
            raise ValueError("Valid branch email and phone are required.")

        with write_transaction(self.session_factory) as session:
            repo = BranchRepository(session)
            branch = repo.get_by_id(branch_id)
            if branch is None:
                raise BranchNotFoundError(f"Branch #{branch_id} was not found.")
            existing_name = repo.get_by_name(name.strip())
            if existing_name is not None and existing_name.id != branch.id:
                raise DuplicateRecordError(f"Branch name '{name.strip()}' already exists.")

            branch.name = name.strip()
            branch.address = address.strip()
            branch.pincode = pincode.strip()
            branch.phone = norm_phone
            branch.email = norm_email
            branch.is_active = bool(is_active)
            branch.updated_at = utc_now()

            AuditRepository(session).record(
                event_type=AuditEventType.BRANCH_MANAGED,
                entity_type="BRANCH",
                entity_id=branch.id,
                actor_user_id=actor_user_id,
                branch_id=branch.id,
                summary=f"Updated branch '{branch.name}' (active={branch.is_active}).",
            )
            return branch

    def list_tiers(self) -> list[MembershipTier]:
        with read_session(self.session_factory) as session:
            return list(TierRepository(session).list_all())

    def update_tier_policy(
        self,
        tier_code: str,
        *,
        max_active_loans: int,
        loan_duration_days: int,
        max_renewals: int,
        renewal_duration_days: int,
        max_active_reservations: int,
        grace_period_days: int,
        daily_fine_rate: Decimal | str,
        max_fine_per_loan: Decimal | str,
        fine_blocking_threshold: Decimal | str,
        borrowing_enabled: bool = True,
        actor_user_id: int | None = None,
    ) -> MembershipTier:
        if max_active_loans < 0 or max_renewals < 0 or max_active_reservations < 0:
            raise ValueError("Loan, renewal, and reservation limits must be non-negative.")
        if loan_duration_days < 1 or renewal_duration_days < 1:
            raise ValueError("Loan and renewal durations must be at least 1 day.")
        if grace_period_days < 0:
            raise ValueError("Grace period days cannot be negative.")

        rate = quantize_money(daily_fine_rate)
        cap = quantize_money(max_fine_per_loan)
        threshold = quantize_money(fine_blocking_threshold)
        if rate < ZERO_MONEY or cap < ZERO_MONEY or threshold < ZERO_MONEY:
            raise ValueError("Monetary policy rates and thresholds cannot be negative.")

        with write_transaction(self.session_factory) as session:
            repo = TierRepository(session)
            tier = repo.get_by_code(tier_code)
            if tier is None:
                raise ValueError(f"Membership tier '{tier_code}' was not found.")

            tier.max_active_loans = int(max_active_loans)
            tier.loan_duration_days = int(loan_duration_days)
            tier.max_renewals = int(max_renewals)
            tier.renewal_duration_days = int(renewal_duration_days)
            tier.max_active_reservations = int(max_active_reservations)
            tier.grace_period_days = int(grace_period_days)
            tier.daily_fine_rate = rate
            tier.max_fine_per_loan = cap
            tier.fine_blocking_threshold = threshold
            tier.borrowing_enabled = bool(borrowing_enabled)
            tier.updated_at = utc_now()

            AuditRepository(session).record(
                event_type=AuditEventType.TIER_POLICY_UPDATED,
                entity_type="MEMBERSHIP_TIER",
                entity_id=tier.code,
                actor_user_id=actor_user_id,
                summary=f"Updated policy for {tier.code} tier (max_loans={tier.max_active_loans}, rate=INR {rate}).",
                details={
                    "max_active_loans": tier.max_active_loans,
                    "loan_duration_days": tier.loan_duration_days,
                    "max_renewals": tier.max_renewals,
                    "renewal_duration_days": tier.renewal_duration_days,
                    "grace_period_days": tier.grace_period_days,
                    "daily_fine_rate": str(rate),
                    "max_fine_per_loan": str(cap),
                    "fine_blocking_threshold": str(threshold),
                    "borrowing_enabled": tier.borrowing_enabled,
                },
            )
            return tier

    def list_holidays(
        self, *, year: int | None = None, branch_id: int | None = None
    ) -> list[LibraryHoliday]:
        with read_session(self.session_factory) as session:
            return list(
                HolidayRepository(session).list_holidays(year=year, branch_id=branch_id)
            )

    def create_holiday(
        self,
        *,
        holiday_date: date,
        name: str,
        branch_id: int | None = None,
        description: str | None = None,
        actor_user_id: int | None = None,
    ) -> LibraryHoliday:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Holiday name is required.")

        with write_transaction(self.session_factory) as session:
            if branch_id is not None:
                if BranchRepository(session).get_by_id(branch_id) is None:
                    raise BranchNotFoundError(f"Branch #{branch_id} was not found.")
            repo = HolidayRepository(session)
            if repo.find_existing(holiday_date, branch_id) is not None:
                scope_label = f"branch #{branch_id}" if branch_id else "network-wide"
                raise DuplicateRecordError(
                    f"A {scope_label} holiday already exists on {holiday_date.isoformat()}."
                )
            holiday = LibraryHoliday(
                holiday_date=holiday_date,
                name=clean_name,
                branch_id=branch_id,
                description=description.strip() if description else None,
                is_active=True,
            )
            repo.add(holiday)
            AuditRepository(session).record(
                event_type=AuditEventType.HOLIDAY_MANAGED,
                entity_type="HOLIDAY",
                entity_id=holiday.id,
                actor_user_id=actor_user_id,
                branch_id=branch_id,
                summary=f"Added holiday '{holiday.name}' on {holiday.holiday_date.isoformat()}.",
            )
            return holiday

    def toggle_holiday(
        self, holiday_id: int, *, is_active: bool, actor_user_id: int | None = None
    ) -> LibraryHoliday:
        with write_transaction(self.session_factory) as session:
            repo = HolidayRepository(session)
            holiday = repo.get_by_id(holiday_id)
            if holiday is None:
                raise ValueError(f"Holiday #{holiday_id} was not found.")
            holiday.is_active = bool(is_active)
            holiday.updated_at = utc_now()
            AuditRepository(session).record(
                event_type=AuditEventType.HOLIDAY_MANAGED,
                entity_type="HOLIDAY",
                entity_id=holiday.id,
                actor_user_id=actor_user_id,
                branch_id=holiday.branch_id,
                summary=f"Set holiday '{holiday.name}' ({holiday.holiday_date}) active={holiday.is_active}.",
            )
            return holiday

    def delete_holiday(
        self, holiday_id: int, *, actor_user_id: int | None = None
    ) -> None:
        with write_transaction(self.session_factory) as session:
            repo = HolidayRepository(session)
            holiday = repo.get_by_id(holiday_id)
            if holiday is None:
                raise ValueError(f"Holiday #{holiday_id} was not found.")
            summary_date = holiday.holiday_date.isoformat()
            summary_name = holiday.name
            branch_id = holiday.branch_id
            repo.delete(holiday)
            AuditRepository(session).record(
                event_type=AuditEventType.HOLIDAY_MANAGED,
                entity_type="HOLIDAY",
                entity_id=holiday_id,
                actor_user_id=actor_user_id,
                branch_id=branch_id,
                summary=f"Removed holiday '{summary_name}' ({summary_date}).",
            )

    def admin_reinstate_withdrawn_copy(
        self,
        copy_id: int | str,
        *,
        reason: str,
        actor_user_id: int,
    ) -> BookCopy:
        """Explicit administrative correction reinstating a WITHDRAWN/LOST/DAMAGED copy to AVAILABLE."""
        clean_reason = reason.strip()
        if len(clean_reason) < 5:
            raise ValueError(
                "An administrative correction reason (at least 5 characters) is required."
            )
        with write_transaction(self.session_factory) as session:
            copy_repo = CopyRepository(session)
            if isinstance(copy_id, int) or str(copy_id).strip().isdigit():
                copy = copy_repo.get_by_id(int(str(copy_id).strip()))
            else:
                copy = copy_repo.get_by_accession(str(copy_id).strip())
            if copy is None:
                raise CopyNotFoundError(f"Copy '{copy_id}' was not found.")
            if LoanRepository(session).get_active_loan_for_copy(copy.id) is not None:
                raise InvalidStateTransitionError(
                    f"Copy {copy.accession_number} has an active loan and cannot be reinstated until closed."
                )
            if copy.status_enum not in (
                CopyStatus.LOST,
                CopyStatus.DAMAGED,
                CopyStatus.WITHDRAWN,
            ):
                raise InvalidStateTransitionError(
                    f"Only LOST, DAMAGED, or WITHDRAWN copies require reinstatement (current status: {copy.status})."
                )
            prev_status = copy.status
            copy.transition_to(
                CopyStatus.AVAILABLE,
                manual=True,
                admin_override=True,
                note=f"[ADMIN CORRECTION] {clean_reason}",
            )
            AuditRepository(session).record(
                event_type=AuditEventType.ADMIN_CORRECTION,
                entity_type="BOOK_COPY",
                entity_id=copy.accession_number,
                actor_user_id=actor_user_id,
                branch_id=copy.owning_branch_id,
                summary=f"Admin reinstated copy {copy.accession_number} from {prev_status} to AVAILABLE.",
                details={
                    "previous_status": prev_status,
                    "new_status": CopyStatus.AVAILABLE.value,
                    "reason": clean_reason,
                },
            )
            return copy

    def admin_adjust_loan_due_date(
        self,
        loan_id: int | str,
        *,
        new_due_date: date,
        reason: str,
        actor_user_id: int,
    ) -> Loan:
        """Explicit administrative correction adjusting an active loan's due date."""
        clean_reason = reason.strip()
        if len(clean_reason) < 5:
            raise ValueError(
                "An administrative correction reason (at least 5 characters) is required."
            )
        with write_transaction(self.session_factory) as session:
            loan_repo = LoanRepository(session)
            if isinstance(loan_id, int) or str(loan_id).strip().isdigit():
                loan = loan_repo.get_by_id(int(str(loan_id).strip()))
            else:
                loan = loan_repo.get_by_loan_number(str(loan_id).strip())
            if loan is None:
                raise LoanNotFoundError(f"Loan '{loan_id}' was not found.")
            if loan.status_enum != LoanStatus.ACTIVE:
                raise ValueError("Only ACTIVE loans can have their due date adjusted.")
            checkout_local_date = to_local_date(
                loan.checked_out_at, self.library_timezone
            )
            if new_due_date < checkout_local_date:
                raise ValueError(
                    f"Adjusted due date ({new_due_date}) cannot precede checkout date ({checkout_local_date})."
                )
            old_due = loan.due_date
            loan.due_date = new_due_date
            loan.updated_at = utc_now()
            AuditRepository(session).record(
                event_type=AuditEventType.ADMIN_CORRECTION,
                entity_type="LOAN",
                entity_id=loan.loan_number,
                actor_user_id=actor_user_id,
                branch_id=loan.issuing_branch_id,
                summary=f"Admin adjusted due date of {loan.loan_number} from {old_due} to {new_due_date}.",
                details={
                    "old_due_date": old_due.isoformat(),
                    "new_due_date": new_due_date.isoformat(),
                    "reason": clean_reason,
                },
            )
            return loan

    def search_audit_events(
        self,
        *,
        event_type: str | None = None,
        entity_type: str | None = None,
        branch_id: int | None = None,
        page: int = 1,
        per_page: int = 30,
    ) -> Page[AuditEvent]:
        with read_session(self.session_factory) as session:
            return AuditRepository(session).search(
                event_type=event_type,
                entity_type=entity_type,
                branch_id=branch_id,
                page=page,
                per_page=per_page,
            )

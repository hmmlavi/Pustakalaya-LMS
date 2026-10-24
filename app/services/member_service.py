"""Member registration, status/tier management, and borrowing eligibility service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Sequence

from sqlalchemy.orm import sessionmaker

from app.domain.enums import AuditEventType, MembershipStatus
from app.domain.exceptions import (
    BranchNotFoundError,
    DuplicateRecordError,
    MemberNotFoundError,
)
from app.domain.policies import EligibilityReport, PolicyFactory
from app.domain.value_objects import (
    local_today,
    normalize_email,
    normalize_phone,
    utc_now,
)
from app.infrastructure.transactions import read_session, write_transaction
from app.models import Fine, FinePayment, Loan, Member, MembershipTier, Reservation
from app.repositories import (
    AuditRepository,
    BranchRepository,
    FineRepository,
    LoanRepository,
    MemberRepository,
    Page,
    ReservationRepository,
    TierRepository,
)


@dataclass(frozen=True, slots=True)
class MemberProfileView:
    member: Member
    eligibility: EligibilityReport
    active_loans: Sequence[Loan]
    loan_history: Sequence[Loan]
    reservations: Sequence[Reservation]
    fines: Sequence[Fine]
    payments: Sequence[FinePayment]
    outstanding_balance: Decimal


class MemberService:
    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        library_timezone: str = "Asia/Kolkata",
    ) -> None:
        self.session_factory = session_factory
        self.library_timezone = library_timezone

    def register_member(
        self,
        *,
        full_name: str,
        tier_code: str = "STANDARD",
        email: str | None = None,
        phone: str | None = None,
        address: str | None = None,
        home_branch_id: int | None = None,
        registration_date: date | None = None,
        expiry_date: date | None = None,
        status: MembershipStatus = MembershipStatus.ACTIVE,
        notes: str | None = None,
        member_code: str | None = None,
        actor_user_id: int | None = None,
    ) -> Member:
        clean_name = " ".join(full_name.strip().split())
        if not clean_name or len(clean_name) > 160:
            raise ValueError("Member full name is required (1-160 characters).")
        norm_email = normalize_email(email)
        norm_phone = normalize_phone(phone)
        reg_date = registration_date or local_today(self.library_timezone)
        exp_date = expiry_date if expiry_date is not None else (reg_date + timedelta(days=365))
        if exp_date < reg_date:
            raise ValueError("Membership expiry date cannot precede registration date.")

        with write_transaction(self.session_factory) as session:
            tier_repo = TierRepository(session)
            tier = tier_repo.get_by_code(tier_code)
            if tier is None:
                raise ValueError(f"Invalid membership tier '{tier_code}'.")

            if home_branch_id is not None:
                if BranchRepository(session).get_by_id(home_branch_id) is None:
                    raise BranchNotFoundError(f"Branch #{home_branch_id} was not found.")

            member_repo = MemberRepository(session)
            if norm_email and member_repo.get_by_email(norm_email) is not None:
                raise DuplicateRecordError(
                    f"A member with email '{norm_email}' is already registered."
                )

            assigned_code = (
                member_code.strip().upper()
                if member_code and member_code.strip()
                else member_repo.next_member_code()
            )
            if member_repo.get_by_member_code(assigned_code) is not None:
                raise DuplicateRecordError(
                    f"Member code '{assigned_code}' already exists."
                )

            member = Member(
                member_code=assigned_code,
                full_name=clean_name,
                email=norm_email,
                phone=norm_phone,
                address=address.strip() if address and address.strip() else None,
                home_branch_id=home_branch_id,
                tier_id=tier.id,
                status=status.value,
                registration_date=reg_date,
                expiry_date=exp_date,
                notes=notes.strip() if notes and notes.strip() else None,
            )
            member_repo.add(member)

            AuditRepository(session).record(
                event_type=AuditEventType.MEMBER_REGISTERED,
                entity_type="MEMBER",
                entity_id=member.member_code,
                actor_user_id=actor_user_id,
                branch_id=home_branch_id,
                summary=f"Registered member {member.member_code} ({member.full_name}) on {tier.code} tier.",
                details={"tier": tier.code, "status": member.status},
            )
            return member_repo.get_by_id(member.id) or member

    def update_member(
        self,
        member_id: int,
        *,
        full_name: str,
        tier_code: str,
        email: str | None = None,
        phone: str | None = None,
        address: str | None = None,
        home_branch_id: int | None = None,
        expiry_date: date | None = None,
        notes: str | None = None,
        actor_user_id: int | None = None,
    ) -> Member:
        clean_name = " ".join(full_name.strip().split())
        if not clean_name or len(clean_name) > 160:
            raise ValueError("Member full name is required (1-160 characters).")
        norm_email = normalize_email(email)
        norm_phone = normalize_phone(phone)

        with write_transaction(self.session_factory) as session:
            member_repo = MemberRepository(session)
            member = member_repo.get_by_id(member_id)
            if member is None:
                raise MemberNotFoundError(f"Member #{member_id} was not found.")

            tier = TierRepository(session).get_by_code(tier_code)
            if tier is None:
                raise ValueError(f"Invalid membership tier '{tier_code}'.")

            if home_branch_id is not None:
                if BranchRepository(session).get_by_id(home_branch_id) is None:
                    raise BranchNotFoundError(f"Branch #{home_branch_id} was not found.")

            if norm_email:
                existing = member_repo.get_by_email(norm_email)
                if existing is not None and existing.id != member.id:
                    raise DuplicateRecordError(
                        f"Email '{norm_email}' is already used by member {existing.member_code}."
                    )
            if expiry_date is not None and expiry_date < member.registration_date:
                raise ValueError("Membership expiry date cannot precede registration date.")

            member.full_name = clean_name
            member.tier_id = tier.id
            member.email = norm_email
            member.phone = norm_phone
            member.address = address.strip() if address and address.strip() else None
            member.home_branch_id = home_branch_id
            member.expiry_date = expiry_date
            member.notes = notes.strip() if notes and notes.strip() else None
            member.updated_at = utc_now()

            AuditRepository(session).record(
                event_type=AuditEventType.MEMBER_UPDATED,
                entity_type="MEMBER",
                entity_id=member.member_code,
                actor_user_id=actor_user_id,
                branch_id=member.home_branch_id,
                summary=f"Updated member {member.member_code} ({member.full_name}).",
                details={"tier": tier.code},
            )
            return member_repo.get_by_id(member.id) or member

    def change_membership_status(
        self,
        member_id: int,
        new_status: MembershipStatus | str,
        *,
        reason: str | None = None,
        actor_user_id: int | None = None,
    ) -> Member:
        status_enum = (
            new_status
            if isinstance(new_status, MembershipStatus)
            else MembershipStatus(new_status.strip().upper())
        )
        with write_transaction(self.session_factory) as session:
            member_repo = MemberRepository(session)
            member = member_repo.get_by_id(member_id)
            if member is None:
                raise MemberNotFoundError(f"Member #{member_id} was not found.")
            prev_status = member.status
            member.status = status_enum.value
            if reason and reason.strip():
                existing = (member.notes or "").strip()
                note_line = f"[STATUS {prev_status}->{status_enum.value}] {reason.strip()}"
                member.notes = f"{existing}\n{note_line}".strip() if existing else note_line
            member.updated_at = utc_now()

            AuditRepository(session).record(
                event_type=AuditEventType.MEMBER_STATUS_CHANGED,
                entity_type="MEMBER",
                entity_id=member.member_code,
                actor_user_id=actor_user_id,
                branch_id=member.home_branch_id,
                summary=f"Changed member {member.member_code} status from {prev_status} to {status_enum.value}.",
                details={
                    "previous_status": prev_status,
                    "new_status": status_enum.value,
                    "reason": reason,
                },
            )
            return member_repo.get_by_id(member.id) or member

    def evaluate_member_eligibility_in_session(
        self, session, member: Member, *, as_of_date: date | None = None
    ) -> EligibilityReport:
        today = as_of_date or local_today(self.library_timezone)
        loan_repo = LoanRepository(session)
        fine_repo = FineRepository(session)

        active_loans = loan_repo.count_active_for_member(member.id)
        overdue_loans = loan_repo.count_active_overdue_for_member(member.id, today)
        unpaid_balance = fine_repo.get_member_outstanding_balance(member.id)

        policy = PolicyFactory.create_borrowing_policy(member.tier)
        return policy.evaluate_eligibility(
            member_status=member.status_enum,
            expiry_date=member.expiry_date,
            active_loans_count=active_loans,
            active_overdue_count=overdue_loans,
            outstanding_fine_balance=unpaid_balance,
            as_of_date=today,
        )

    def get_member_eligibility(
        self, member_id_or_code: int | str, *, as_of_date: date | None = None
    ) -> EligibilityReport:
        with read_session(self.session_factory) as session:
            repo = MemberRepository(session)
            member = (
                repo.get_by_id(member_id_or_code)
                if isinstance(member_id_or_code, int)
                else repo.get_by_member_code(str(member_id_or_code))
            )
            if member is None:
                raise MemberNotFoundError(f"Member '{member_id_or_code}' was not found.")
            return self.evaluate_member_eligibility_in_session(
                session, member, as_of_date=as_of_date
            )

    def get_member(self, member_id: int) -> Member:
        with read_session(self.session_factory) as session:
            member = MemberRepository(session).get_by_id(member_id)
            if member is None:
                raise MemberNotFoundError(f"Member #{member_id} was not found.")
            return member

    def get_member_profile(
        self, member_id: int, *, as_of_date: date | None = None
    ) -> MemberProfileView:
        today = as_of_date or local_today(self.library_timezone)
        with read_session(self.session_factory) as session:
            member = MemberRepository(session).get_by_id(member_id)
            if member is None:
                raise MemberNotFoundError(f"Member #{member_id} was not found.")
            loan_repo = LoanRepository(session)
            res_repo = ReservationRepository(session)
            fine_repo = FineRepository(session)

            eligibility = self.evaluate_member_eligibility_in_session(
                session, member, as_of_date=today
            )
            active_loans = loan_repo.list_for_member(member.id, active_only=True)
            loan_history = loan_repo.list_for_member(member.id, active_only=False, limit=100)
            reservations = res_repo.list_for_member(member.id, limit=50)
            fines = fine_repo.list_for_member(member.id)
            payments = fine_repo.list_payments_for_member(member.id)
            balance = fine_repo.get_member_outstanding_balance(member.id)

            return MemberProfileView(
                member=member,
                eligibility=eligibility,
                active_loans=active_loans,
                loan_history=loan_history,
                reservations=reservations,
                fines=fines,
                payments=payments,
                outstanding_balance=balance,
            )

    def list_tiers(self) -> list[MembershipTier]:
        with read_session(self.session_factory) as session:
            return list(TierRepository(session).list_all())

    def search_members(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        tier_code: str | None = None,
        branch_id: int | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> Page[Member]:
        with read_session(self.session_factory) as session:
            return MemberRepository(session).search(
                query=query,
                status=status,
                tier_code=tier_code,
                branch_id=branch_id,
                page=page,
                per_page=per_page,
            )

"""Business policy abstractions using the Strategy and Factory patterns.

Contains zero web or database-driver dependencies so every circulation,
membership-tier, due-date, and fine rule can be tested purely in memory.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Protocol

from app.domain.enums import MembershipStatus
from app.domain.exceptions import (
    BorrowingLimitExceededError,
    MemberNotEligibleError,
    OutstandingFineError,
    RenewalLimitExceededError,
    RenewalNotAllowedError,
    ReservationConflictError,
)
from app.domain.value_objects import ZERO_MONEY, quantize_money


class TierConfigLike(Protocol):
    """Protocol satisfied by both MembershipTier entities and pure config dataclasses."""

    code: str
    name: str
    max_active_loans: int
    loan_duration_days: int
    max_renewals: int
    renewal_duration_days: int
    max_active_reservations: int
    grace_period_days: int
    daily_fine_rate: Decimal
    max_fine_per_loan: Decimal
    fine_blocking_threshold: Decimal
    borrowing_enabled: bool


@dataclass(frozen=True, slots=True)
class TierPolicyConfig:
    """Immutable value snapshot of a membership tier's policy parameters."""

    code: str
    name: str
    max_active_loans: int
    loan_duration_days: int
    max_renewals: int
    renewal_duration_days: int
    max_active_reservations: int
    grace_period_days: int
    daily_fine_rate: Decimal
    max_fine_per_loan: Decimal
    fine_blocking_threshold: Decimal
    borrowing_enabled: bool = True


@dataclass(frozen=True, slots=True)
class HolidayEntry:
    """Lightweight value representation of an active library holiday."""

    holiday_date: date
    name: str
    branch_id: int | None = None


class HolidayCalendar:
    """Local holiday calendar supporting network-wide and branch-specific closures."""

    def __init__(self, holidays: Iterable[HolidayEntry] = ()) -> None:
        self._network_dates: set[date] = set()
        self._branch_dates: dict[int, set[date]] = {}
        for item in holidays:
            if item.branch_id is None:
                self._network_dates.add(item.holiday_date)
            else:
                self._branch_dates.setdefault(item.branch_id, set()).add(item.holiday_date)

    def is_holiday(self, target_date: date, branch_id: int | None = None) -> bool:
        if target_date in self._network_dates:
            return True
        if branch_id is not None and target_date in self._branch_dates.get(branch_id, set()):
            return True
        return False

    def adjust_to_operating_day(
        self, candidate_date: date, branch_id: int | None = None
    ) -> date:
        """Roll a candidate due/pickup date forward to the next non-holiday operating day."""
        current = candidate_date
        for _ in range(366):
            if not self.is_holiday(current, branch_id=branch_id):
                return current
            current += timedelta(days=1)
        raise RuntimeError("No operating day found within 366 days; check holiday configuration.")

    def count_operating_days_after(
        self,
        due_date: date,
        effective_return_date: date,
        branch_id: int | None = None,
    ) -> tuple[int, int]:
        """Count (chargeable_operating_days, excluded_holiday_days) in (due_date, effective_return_date]."""
        if effective_return_date <= due_date:
            return 0, 0
        chargeable = 0
        holidays_skipped = 0
        cursor = due_date + timedelta(days=1)
        while cursor <= effective_return_date:
            if self.is_holiday(cursor, branch_id=branch_id):
                holidays_skipped += 1
            else:
                chargeable += 1
            cursor += timedelta(days=1)
        return chargeable, holidays_skipped


@dataclass(frozen=True, slots=True)
class EligibilityReport:
    """Detailed explanation of a member's current borrowing eligibility."""

    eligible: bool
    reasons: tuple[str, ...]
    active_loans: int
    max_active_loans: int
    remaining_loan_slots: int
    outstanding_fines: Decimal
    fine_blocking_threshold: Decimal
    active_overdue_loans: int


@dataclass(frozen=True, slots=True)
class FineBreakdown:
    """Deterministic explanation of an overdue fine calculation."""

    due_date: date
    effective_return_date: date
    calendar_days_late: int
    holiday_days_excluded: int
    chargeable_days_after_due: int
    grace_period_days: int
    eligible_overdue_days: int
    daily_rate: Decimal
    uncapped_amount: Decimal
    max_cap: Decimal
    assessed_amount: Decimal


class BorrowingPolicyStrategy(ABC):
    """Strategy interface governing member borrowing, renewal, and reservation rules."""

    @abstractmethod
    def evaluate_eligibility(
        self,
        *,
        member_status: MembershipStatus,
        expiry_date: date | None,
        active_loans_count: int,
        active_overdue_count: int,
        outstanding_fine_balance: Decimal,
        as_of_date: date,
    ) -> EligibilityReport:
        """Evaluate eligibility without raising, for UI display and diagnostics."""

    @abstractmethod
    def assert_can_issue(
        self,
        *,
        member_status: MembershipStatus,
        expiry_date: date | None,
        active_loans_count: int,
        active_overdue_count: int,
        outstanding_fine_balance: Decimal,
        as_of_date: date,
    ) -> None:
        """Raise the specific domain exception if member cannot borrow another copy."""

    @abstractmethod
    def assert_can_renew(
        self,
        *,
        member_status: MembershipStatus,
        expiry_date: date | None,
        outstanding_fine_balance: Decimal,
        current_renewal_count: int,
        current_due_date: date,
        has_competing_reservation: bool,
        as_of_date: date,
    ) -> None:
        """Raise the specific domain exception if loan cannot be renewed."""

    @abstractmethod
    def assert_can_reserve(
        self,
        *,
        member_status: MembershipStatus,
        expiry_date: date | None,
        outstanding_fine_balance: Decimal,
        active_reservations_count: int,
        as_of_date: date,
    ) -> None:
        """Raise the specific domain exception if member cannot place a reservation."""

    @abstractmethod
    def calculate_initial_due_date(
        self,
        checkout_date: date,
        calendar: HolidayCalendar,
        branch_id: int | None = None,
    ) -> date:
        """Compute initial due date shifted past any closing holiday."""

    @abstractmethod
    def calculate_renewal_due_date(
        self,
        current_due_date: date,
        calendar: HolidayCalendar,
        branch_id: int | None = None,
    ) -> date:
        """Compute renewed due date shifted past any closing holiday."""


class TierBorrowingPolicy(BorrowingPolicyStrategy):
    """Configurable tier-driven implementation of BorrowingPolicyStrategy."""

    def __init__(self, config: TierPolicyConfig) -> None:
        self.config = config

    def _check_base_member_standing(
        self,
        *,
        member_status: MembershipStatus,
        expiry_date: date | None,
        outstanding_fine_balance: Decimal,
        as_of_date: date,
    ) -> None:
        if not self.config.borrowing_enabled:
            raise MemberNotEligibleError(
                f"Borrowing privileges are currently disabled for the {self.config.name} tier."
            )
        if member_status != MembershipStatus.ACTIVE:
            raise MemberNotEligibleError(
                f"Member status is {member_status.value}; only ACTIVE members may borrow or reserve items."
            )
        if expiry_date is not None and expiry_date < as_of_date:
            raise MemberNotEligibleError(
                f"Membership expired on {expiry_date.isoformat()}. Please renew membership before borrowing."
            )
        balance = quantize_money(outstanding_fine_balance)
        threshold = quantize_money(self.config.fine_blocking_threshold)
        if threshold <= ZERO_MONEY:
            if balance > ZERO_MONEY:
                raise OutstandingFineError(
                    f"Member has unpaid fines of INR {balance} (zero-tolerance threshold for {self.config.code})."
                )
        elif balance >= threshold:
            raise OutstandingFineError(
                f"Member unpaid fine balance INR {balance} meets or exceeds the "
                f"{self.config.code} blocking threshold of INR {threshold}."
            )

    def evaluate_eligibility(
        self,
        *,
        member_status: MembershipStatus,
        expiry_date: date | None,
        active_loans_count: int,
        active_overdue_count: int,
        outstanding_fine_balance: Decimal,
        as_of_date: date,
    ) -> EligibilityReport:
        reasons: list[str] = []
        balance = quantize_money(outstanding_fine_balance)
        threshold = quantize_money(self.config.fine_blocking_threshold)

        if not self.config.borrowing_enabled:
            reasons.append(f"Tier '{self.config.name}' has borrowing disabled.")
        if member_status != MembershipStatus.ACTIVE:
            reasons.append(f"Membership status is {member_status.value}.")
        if expiry_date is not None and expiry_date < as_of_date:
            reasons.append(f"Membership expired on {expiry_date.isoformat()}.")
        if active_overdue_count > 0:
            reasons.append(
                f"Member currently holds {active_overdue_count} overdue active loan(s)."
            )
        if (threshold <= ZERO_MONEY and balance > ZERO_MONEY) or (
            threshold > ZERO_MONEY and balance >= threshold
        ):
            reasons.append(
                f"Outstanding fines (INR {balance}) meet/exceed blocking threshold (INR {threshold})."
            )
        if active_loans_count >= self.config.max_active_loans:
            reasons.append(
                f"Borrowing limit reached ({active_loans_count}/{self.config.max_active_loans} active loans)."
            )

        remaining = max(0, self.config.max_active_loans - active_loans_count)
        return EligibilityReport(
            eligible=len(reasons) == 0,
            reasons=tuple(reasons),
            active_loans=active_loans_count,
            max_active_loans=self.config.max_active_loans,
            remaining_loan_slots=remaining,
            outstanding_fines=balance,
            fine_blocking_threshold=threshold,
            active_overdue_loans=active_overdue_count,
        )

    def assert_can_issue(
        self,
        *,
        member_status: MembershipStatus,
        expiry_date: date | None,
        active_loans_count: int,
        active_overdue_count: int,
        outstanding_fine_balance: Decimal,
        as_of_date: date,
    ) -> None:
        self._check_base_member_standing(
            member_status=member_status,
            expiry_date=expiry_date,
            outstanding_fine_balance=outstanding_fine_balance,
            as_of_date=as_of_date,
        )
        if active_overdue_count > 0:
            raise MemberNotEligibleError(
                f"Member has {active_overdue_count} overdue loan(s); overdue copies must be returned before issuing new books."
            )
        if active_loans_count >= self.config.max_active_loans:
            raise BorrowingLimitExceededError(
                f"Member already has {active_loans_count} active loan(s), reaching the "
                f"{self.config.code} tier limit of {self.config.max_active_loans}."
            )

    def assert_can_renew(
        self,
        *,
        member_status: MembershipStatus,
        expiry_date: date | None,
        outstanding_fine_balance: Decimal,
        current_renewal_count: int,
        current_due_date: date,
        has_competing_reservation: bool,
        as_of_date: date,
    ) -> None:
        self._check_base_member_standing(
            member_status=member_status,
            expiry_date=expiry_date,
            outstanding_fine_balance=outstanding_fine_balance,
            as_of_date=as_of_date,
        )
        if as_of_date > current_due_date:
            raise RenewalNotAllowedError(
                f"Loan became overdue after {current_due_date.isoformat()}; overdue loans cannot be renewed and must be returned."
            )
        if current_renewal_count >= self.config.max_renewals:
            raise RenewalLimitExceededError(
                f"Maximum renewal limit ({self.config.max_renewals}) for {self.config.code} tier has been reached."
            )
        if has_competing_reservation:
            raise ReservationConflictError(
                "Renewal denied because another member holds an active reservation for this title."
            )

    def assert_can_reserve(
        self,
        *,
        member_status: MembershipStatus,
        expiry_date: date | None,
        outstanding_fine_balance: Decimal,
        active_reservations_count: int,
        as_of_date: date,
    ) -> None:
        self._check_base_member_standing(
            member_status=member_status,
            expiry_date=expiry_date,
            outstanding_fine_balance=outstanding_fine_balance,
            as_of_date=as_of_date,
        )
        if active_reservations_count >= self.config.max_active_reservations:
            raise ReservationConflictError(
                f"Member already has {active_reservations_count} active reservation(s), "
                f"reaching the {self.config.code} tier limit of {self.config.max_active_reservations}."
            )

    def calculate_initial_due_date(
        self,
        checkout_date: date,
        calendar: HolidayCalendar,
        branch_id: int | None = None,
    ) -> date:
        raw_due = checkout_date + timedelta(days=self.config.loan_duration_days)
        return calendar.adjust_to_operating_day(raw_due, branch_id=branch_id)

    def calculate_renewal_due_date(
        self,
        current_due_date: date,
        calendar: HolidayCalendar,
        branch_id: int | None = None,
    ) -> date:
        raw_due = current_due_date + timedelta(days=self.config.renewal_duration_days)
        return calendar.adjust_to_operating_day(raw_due, branch_id=branch_id)


class FineCalculationStrategy(ABC):
    """Strategy interface for overdue fine calculation."""

    @abstractmethod
    def calculate_fine(
        self,
        *,
        due_date: date,
        effective_return_date: date,
        checkout_date: date | None = None,
        branch_id: int | None = None,
    ) -> FineBreakdown:
        """Compute deterministic overdue fine breakdown."""


class HolidayAwareFinePolicy(FineCalculationStrategy):
    """Holiday- and grace-period-aware Decimal fine calculation policy.

    Formula:
        1. If effective_return_date <= due_date -> INR 0.00.
        2. Count chargeable operating days strictly after due_date up to effective_return_date,
           excluding network-wide and branch-specific library holidays.
        3. If chargeable_days <= grace_period_days -> INR 0.00.
        4. Otherwise eligible_overdue_days = chargeable_days - grace_period_days.
        5. uncapped = quantize_money(eligible_overdue_days * daily_fine_rate).
        6. assessed = min(uncapped, max_fine_per_loan).
    """

    def __init__(self, config: TierPolicyConfig, calendar: HolidayCalendar) -> None:
        self.config = config
        self.calendar = calendar

    def calculate_fine(
        self,
        *,
        due_date: date,
        effective_return_date: date,
        checkout_date: date | None = None,
        branch_id: int | None = None,
    ) -> FineBreakdown:
        if checkout_date is not None:
            if due_date < checkout_date:
                raise ValueError(
                    f"Invalid loan dates: due_date ({due_date}) precedes checkout_date ({checkout_date})."
                )
            if effective_return_date < checkout_date:
                raise ValueError(
                    f"Invalid loan dates: return_date ({effective_return_date}) precedes checkout_date ({checkout_date})."
                )

        daily_rate = quantize_money(self.config.daily_fine_rate)
        max_cap = quantize_money(self.config.max_fine_per_loan)
        grace_days = max(0, int(self.config.grace_period_days))

        if effective_return_date <= due_date:
            return FineBreakdown(
                due_date=due_date,
                effective_return_date=effective_return_date,
                calendar_days_late=0,
                holiday_days_excluded=0,
                chargeable_days_after_due=0,
                grace_period_days=grace_days,
                eligible_overdue_days=0,
                daily_rate=daily_rate,
                uncapped_amount=ZERO_MONEY,
                max_cap=max_cap,
                assessed_amount=ZERO_MONEY,
            )

        calendar_days_late = (effective_return_date - due_date).days
        chargeable_days, holiday_days = self.calendar.count_operating_days_after(
            due_date=due_date,
            effective_return_date=effective_return_date,
            branch_id=branch_id,
        )

        if chargeable_days <= grace_days:
            eligible_days = 0
        else:
            eligible_days = chargeable_days - grace_days

        uncapped = quantize_money(Decimal(eligible_days) * daily_rate)
        assessed = min(uncapped, max_cap)

        return FineBreakdown(
            due_date=due_date,
            effective_return_date=effective_return_date,
            calendar_days_late=calendar_days_late,
            holiday_days_excluded=holiday_days,
            chargeable_days_after_due=chargeable_days,
            grace_period_days=grace_days,
            eligible_overdue_days=eligible_days,
            daily_rate=daily_rate,
            uncapped_amount=uncapped,
            max_cap=max_cap,
            assessed_amount=assessed,
        )


class PolicyFactory:
    """Factory constructing configured borrowing and fine strategies from tier entities."""

    @staticmethod
    def to_config(tier: TierConfigLike) -> TierPolicyConfig:
        return TierPolicyConfig(
            code=str(tier.code),
            name=tier.name,
            max_active_loans=int(tier.max_active_loans),
            loan_duration_days=int(tier.loan_duration_days),
            max_renewals=int(tier.max_renewals),
            renewal_duration_days=int(tier.renewal_duration_days),
            max_active_reservations=int(tier.max_active_reservations),
            grace_period_days=int(tier.grace_period_days),
            daily_fine_rate=quantize_money(tier.daily_fine_rate),
            max_fine_per_loan=quantize_money(tier.max_fine_per_loan),
            fine_blocking_threshold=quantize_money(tier.fine_blocking_threshold),
            borrowing_enabled=bool(tier.borrowing_enabled),
        )

    @classmethod
    def create_borrowing_policy(cls, tier: TierConfigLike) -> TierBorrowingPolicy:
        return TierBorrowingPolicy(cls.to_config(tier))

    @classmethod
    def create_fine_policy(
        cls, tier: TierConfigLike, calendar: HolidayCalendar
    ) -> HolidayAwareFinePolicy:
        return HolidayAwareFinePolicy(cls.to_config(tier), calendar)

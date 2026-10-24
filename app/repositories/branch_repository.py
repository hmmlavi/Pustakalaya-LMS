"""Repositories for LibraryBranch and LibraryHoliday persistence."""

from __future__ import annotations

from datetime import date
from typing import Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.domain.policies import HolidayCalendar, HolidayEntry
from app.models import LibraryBranch, LibraryHoliday


class BranchRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, branch_id: int) -> LibraryBranch | None:
        return self.session.get(LibraryBranch, branch_id)

    def get_by_code(self, code: str) -> LibraryBranch | None:
        stmt = select(LibraryBranch).where(
            LibraryBranch.code == code.strip().upper()
        )
        return self.session.scalar(stmt)

    def get_by_name(self, name: str) -> LibraryBranch | None:
        stmt = select(LibraryBranch).where(LibraryBranch.name == name.strip())
        return self.session.scalar(stmt)

    def list_all(self, *, active_only: bool = False) -> Sequence[LibraryBranch]:
        stmt = select(LibraryBranch).order_by(LibraryBranch.id.asc())
        if active_only:
            stmt = stmt.where(LibraryBranch.is_active.is_(True))
        return list(self.session.scalars(stmt).all())

    def add(self, branch: LibraryBranch) -> LibraryBranch:
        self.session.add(branch)
        self.session.flush()
        return branch


class HolidayRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, holiday_id: int) -> LibraryHoliday | None:
        stmt = (
            select(LibraryHoliday)
            .options(joinedload(LibraryHoliday.branch))
            .where(LibraryHoliday.id == holiday_id)
        )
        return self.session.scalar(stmt)

    def find_existing(
        self, holiday_date: date, branch_id: int | None
    ) -> LibraryHoliday | None:
        stmt = select(LibraryHoliday).where(LibraryHoliday.holiday_date == holiday_date)
        if branch_id is None:
            stmt = stmt.where(LibraryHoliday.branch_id.is_(None))
        else:
            stmt = stmt.where(LibraryHoliday.branch_id == branch_id)
        return self.session.scalar(stmt)

    def list_holidays(
        self,
        *,
        year: int | None = None,
        branch_id: int | None = None,
        active_only: bool = False,
    ) -> Sequence[LibraryHoliday]:
        stmt = (
            select(LibraryHoliday)
            .options(joinedload(LibraryHoliday.branch))
            .order_by(LibraryHoliday.holiday_date.asc(), LibraryHoliday.id.asc())
        )
        if active_only:
            stmt = stmt.where(LibraryHoliday.is_active.is_(True))
        if year is not None:
            stmt = stmt.where(
                LibraryHoliday.holiday_date >= date(year, 1, 1),
                LibraryHoliday.holiday_date <= date(year, 12, 31),
            )
        if branch_id is not None:
            stmt = stmt.where(
                or_(
                    LibraryHoliday.branch_id.is_(None),
                    LibraryHoliday.branch_id == branch_id,
                )
            )
        return list(self.session.scalars(stmt).all())

    def build_calendar(self) -> HolidayCalendar:
        stmt = select(LibraryHoliday).where(LibraryHoliday.is_active.is_(True))
        rows = self.session.scalars(stmt).all()
        entries = [
            HolidayEntry(
                holiday_date=row.holiday_date,
                name=row.name,
                branch_id=row.branch_id,
            )
            for row in rows
        ]
        return HolidayCalendar(entries)

    def add(self, holiday: LibraryHoliday) -> LibraryHoliday:
        self.session.add(holiday)
        self.session.flush()
        return holiday

    def delete(self, holiday: LibraryHoliday) -> None:
        self.session.delete(holiday)
        self.session.flush()

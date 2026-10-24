"""Repositories for Member and MembershipTier persistence."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import Member, MembershipTier
from app.repositories.base import Page, escape_like, paginate_statement


class TierRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, tier_id: int) -> MembershipTier | None:
        return self.session.get(MembershipTier, tier_id)

    def get_by_code(self, code: str) -> MembershipTier | None:
        stmt = select(MembershipTier).where(
            MembershipTier.code == code.strip().upper()
        )
        return self.session.scalar(stmt)

    def list_all(self) -> Sequence[MembershipTier]:
        stmt = select(MembershipTier).order_by(MembershipTier.max_active_loans.asc())
        return list(self.session.scalars(stmt).all())

    def add(self, tier: MembershipTier) -> MembershipTier:
        self.session.add(tier)
        self.session.flush()
        return tier


class MemberRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, member_id: int) -> Member | None:
        stmt = (
            select(Member)
            .options(joinedload(Member.tier), joinedload(Member.home_branch))
            .where(Member.id == member_id)
        )
        return self.session.scalar(stmt)

    def get_by_member_code(self, member_code: str) -> Member | None:
        cleaned = member_code.strip().upper()
        stmt = (
            select(Member)
            .options(joinedload(Member.tier), joinedload(Member.home_branch))
            .where(func.upper(Member.member_code) == cleaned)
        )
        return self.session.scalar(stmt)

    def get_by_email(self, normalized_email: str) -> Member | None:
        stmt = select(Member).where(Member.email == normalized_email)
        return self.session.scalar(stmt)

    def next_member_code(self) -> str:
        max_id = int(self.session.scalar(select(func.max(Member.id))) or 0)
        candidate_num = max_id + 1
        while True:
            code = f"MEM-{candidate_num:06d}"
            exists = self.session.scalar(
                select(Member.id).where(Member.member_code == code)
            )
            if exists is None:
                return code
            candidate_num += 1

    def search(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        tier_code: str | None = None,
        branch_id: int | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> Page[Member]:
        stmt = (
            select(Member)
            .join(MembershipTier, MembershipTier.id == Member.tier_id)
            .options(joinedload(Member.tier), joinedload(Member.home_branch))
        )
        if query and query.strip():
            like_q = f"%{escape_like(query.strip().lower())}%"
            stmt = stmt.where(
                or_(
                    func.lower(Member.member_code).like(like_q, escape="\\"),
                    func.lower(Member.full_name).like(like_q, escape="\\"),
                    func.lower(func.coalesce(Member.email, "")).like(
                        like_q, escape="\\"
                    ),
                    func.lower(func.coalesce(Member.phone, "")).like(
                        like_q, escape="\\"
                    ),
                )
            )
        if status and status.strip():
            stmt = stmt.where(Member.status == status.strip().upper())
        if tier_code and tier_code.strip():
            stmt = stmt.where(MembershipTier.code == tier_code.strip().upper())
        if branch_id is not None:
            stmt = stmt.where(Member.home_branch_id == branch_id)

        stmt = stmt.order_by(Member.member_code.asc())
        return paginate_statement(self.session, stmt, page=page, per_page=per_page)

    def count_all(self) -> int:
        return int(self.session.scalar(select(func.count(Member.id))) or 0)

    def add(self, member: Member) -> Member:
        self.session.add(member)
        self.session.flush()
        return member

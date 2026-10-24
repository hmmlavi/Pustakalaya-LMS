"""Repository for staff and administrator User accounts."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models import User


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, user_id: int) -> User | None:
        stmt = (
            select(User)
            .options(joinedload(User.branch))
            .where(User.id == user_id)
        )
        return self.session.scalar(stmt)

    def get_by_username(self, username: str) -> User | None:
        cleaned = username.strip().lower()
        stmt = (
            select(User)
            .options(joinedload(User.branch))
            .where(func.lower(User.username) == cleaned)
        )
        return self.session.scalar(stmt)

    def get_by_email(self, email: str) -> User | None:
        cleaned = email.strip().lower()
        stmt = select(User).where(func.lower(User.email) == cleaned)
        return self.session.scalar(stmt)

    def list_all(self) -> Sequence[User]:
        stmt = (
            select(User)
            .options(joinedload(User.branch))
            .order_by(User.username.asc())
        )
        return list(self.session.scalars(stmt).all())

    def count_all(self) -> int:
        return int(self.session.scalar(select(func.count(User.id))) or 0)

    def add(self, user: User) -> User:
        self.session.add(user)
        self.session.flush()
        return user

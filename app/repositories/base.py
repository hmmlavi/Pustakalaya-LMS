"""Shared pagination structure and safe SQL LIKE escaping for repositories."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Generic, Sequence, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """Paginated query result container."""

    items: Sequence[T]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        if self.total <= 0:
            return 1
        return max(1, math.ceil(self.total / self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def prev_num(self) -> int:
        return max(1, self.page - 1)

    @property
    def next_num(self) -> int:
        return min(self.pages, self.page + 1)


def escape_like(term: str) -> str:
    """Escape SQL LIKE wildcards so literal %, _, and \\ characters match safely."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def paginate_statement(
    session: Session,
    stmt: Select,
    *,
    page: int = 1,
    per_page: int = 20,
) -> Page:
    """Execute a count + offset/limit query safely on any SQLAlchemy Select."""
    clean_page = max(1, int(page))
    clean_per_page = min(200, max(1, int(per_page)))
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int(session.scalar(count_stmt) or 0)
    total_pages = max(1, math.ceil(total / clean_per_page)) if total > 0 else 1
    if clean_page > total_pages:
        clean_page = total_pages
    offset = (clean_page - 1) * clean_per_page
    items = list(session.scalars(stmt.offset(offset).limit(clean_per_page)).unique().all())
    return Page(items=items, total=total, page=clean_page, per_page=clean_per_page)

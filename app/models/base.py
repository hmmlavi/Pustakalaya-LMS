"""SQLAlchemy 2.0 declarative base and custom safe column types."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import DateTime, Integer, MetaData
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from app.domain.value_objects import MONEY_QUANT, quantize_money

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class MoneyPaise(TypeDecorator[Decimal]):
    """Store INR Decimal amounts in SQLite as exact integer paise (1 INR = 100 paise).

    Eliminates binary floating-point rounding inside SQLite while preserving native
    integer ordering, CHECK constraints, and SUM() aggregation.
    """

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value: Decimal | int | str | None, dialect) -> int | None:
        if value is None:
            return None
        dec = quantize_money(value)
        paise = int((dec * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))
        return paise

    def process_result_value(self, value: int | None, dialect) -> Decimal | None:
        if value is None:
            return None
        return (Decimal(int(value)) / Decimal(100)).quantize(
            MONEY_QUANT, rounding=ROUND_HALF_UP
        )


class UTCDateTime(TypeDecorator[datetime]):
    """Ensure timezone-aware UTC datetimes round-trip cleanly through SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

"""Domain value objects and normalization utilities (ISBN, Money, Contact, Timezone)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from zoneinfo import ZoneInfo

from app.domain.exceptions import InvalidISBNError

MONEY_QUANT = Decimal("0.01")
ZERO_MONEY = Decimal("0.00")

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_PHONE_CLEAN_RE = re.compile(r"[\s\-().]")


def quantize_money(value: Decimal | int | str) -> Decimal:
    """Convert a Decimal, integer, or string monetary value to exact 2-decimal INR amount.

    Rejects binary float inputs explicitly to prevent IEEE-754 imprecision.
    """
    if isinstance(value, float):
        raise TypeError("Monetary amounts must not be constructed from binary float.")
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid monetary amount: {value!r}") from exc
    if not dec.is_finite():
        raise ValueError("Monetary amount must be finite.")
    return dec.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class ISBN:
    """Validated ISBN-10 or ISBN-13 value object."""

    normalized: str

    @classmethod
    def parse(cls, raw: str | None) -> ISBN | None:
        """Parse and validate an ISBN-10 or ISBN-13 string. Returns None if blank/None."""
        if raw is None:
            return None
        cleaned = raw.strip().replace("-", "").replace(" ", "").upper()
        if not cleaned:
            return None
        if len(cleaned) == 10:
            if not cls.is_valid_isbn10(cleaned):
                raise InvalidISBNError(f"Invalid ISBN-10 checksum or format: '{raw}'.")
            return cls(normalized=cleaned)
        if len(cleaned) == 13:
            if not cls.is_valid_isbn13(cleaned):
                raise InvalidISBNError(f"Invalid ISBN-13 checksum or format: '{raw}'.")
            return cls(normalized=cleaned)
        raise InvalidISBNError(
            f"ISBN '{raw}' must contain 10 or 13 characters after removing hyphens."
        )

    @staticmethod
    def is_valid_isbn10(cleaned: str) -> bool:
        if len(cleaned) != 10:
            return False
        if not cleaned[:9].isdigit():
            return False
        if not (cleaned[9].isdigit() or cleaned[9] == "X"):
            return False
        total = 0
        for idx, ch in enumerate(cleaned):
            digit = 10 if ch == "X" else int(ch)
            total += (10 - idx) * digit
        return total % 11 == 0

    @staticmethod
    def is_valid_isbn13(cleaned: str) -> bool:
        if len(cleaned) != 13 or not cleaned.isdigit():
            return False
        if not (cleaned.startswith("978") or cleaned.startswith("979")):
            return False
        total = sum(
            int(ch) * (1 if idx % 2 == 0 else 3) for idx, ch in enumerate(cleaned)
        )
        return total % 10 == 0

    @staticmethod
    def compute_isbn13_check_digit(prefix12: str) -> str:
        """Compute the 13th check digit for a 12-digit 978/979 prefix."""
        if len(prefix12) != 12 or not prefix12.isdigit():
            raise ValueError("prefix12 must be 12 digits")
        total = sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(prefix12))
        rem = (10 - (total % 10)) % 10
        return str(rem)

    @staticmethod
    def compute_isbn10_check_digit(prefix9: str) -> str:
        """Compute the 10th check digit ('0'-'9' or 'X') for a 9-digit prefix."""
        if len(prefix9) != 9 or not prefix9.isdigit():
            raise ValueError("prefix9 must be 9 digits")
        total = sum((10 - i) * int(ch) for i, ch in enumerate(prefix9))
        rem = (11 - (total % 11)) % 11
        return "X" if rem == 10 else str(rem)

    def __str__(self) -> str:
        return self.normalized


def normalize_email(raw: str | None) -> str | None:
    """Normalize and validate an optional email address."""
    if raw is None:
        return None
    cleaned = raw.strip().lower()
    if not cleaned:
        return None
    if len(cleaned) > 254 or not _EMAIL_RE.match(cleaned):
        raise ValueError(f"Invalid email address format: '{raw}'.")
    return cleaned


def normalize_phone(raw: str | None) -> str | None:
    """Normalize and validate an optional phone number."""
    if raw is None:
        return None
    cleaned = _PHONE_CLEAN_RE.sub("", raw.strip())
    if not cleaned:
        return None
    digits_only = cleaned[1:] if cleaned.startswith("+") else cleaned
    if not digits_only.isdigit() or not (7 <= len(digits_only) <= 15):
        raise ValueError(f"Invalid phone number format: '{raw}'.")
    return cleaned


def utc_now() -> datetime:
    """Return current timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def local_today(tz_name: str = "Asia/Kolkata") -> date:
    """Return today's operational calendar date in the library's local timezone."""
    return datetime.now(ZoneInfo(tz_name)).date()


def to_local_date(dt: datetime, tz_name: str = "Asia/Kolkata") -> date:
    """Convert a timezone-aware datetime to the library's operational local date."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz_name)).date()


def end_of_local_day_utc(local_day: date, tz_name: str = "Asia/Kolkata") -> datetime:
    """Return 23:59:59 in the library's local timezone converted to UTC."""
    local_dt = datetime.combine(local_day, time(23, 59, 59), tzinfo=ZoneInfo(tz_name))
    return local_dt.astimezone(timezone.utc)

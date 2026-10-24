"""Rich persistent domain entities mapped with SQLAlchemy 2.0.

Contains zero dependency on Flask, web forms, or HTML templates. Every class
can be instantiated and exercised purely in memory for unit tests while also
mapping cleanly to normalized relational tables with database-enforced invariants.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import json
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from werkzeug.security import check_password_hash, generate_password_hash

from app.domain.enums import (
    CopyStatus,
    FineStatus,
    LoanStatus,
    MembershipStatus,
    PaymentMethod,
    ReservationStatus,
    TierCode,
    UserRole,
)
from app.domain.exceptions import (
    InvalidFinePaymentError,
    LoanAlreadyReturnedError,
)
from app.domain.state_machines import CopyStateMachine, ReservationStateMachine
from app.domain.value_objects import (
     ISBN,
    ZERO_MONEY,
    normalize_email,
    normalize_phone,
    quantize_money,
    utc_now,
)
from app.models.base import Base, MoneyPaise, UTCDateTime


class LibraryBranch(Base):
    """Represents one of the public library branches in Lucknow."""

    __tablename__ = "branches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    address: Mapped[str] = mapped_column(String(300), nullable=False)
    city: Mapped[str] = mapped_column(String(80), nullable=False, default="Lucknow")
    state: Mapped[str] = mapped_column(String(80), nullable=False, default="Uttar Pradesh")
    pincode: Mapped[str] = mapped_column(String(10), nullable=False)
    phone: Mapped[str] = mapped_column(String(30), nullable=False)
    email: Mapped[str] = mapped_column(String(150), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    copies: Mapped[list["BookCopy"]] = relationship(
        "BookCopy", back_populates="owning_branch"
    )
    holidays: Mapped[list["LibraryHoliday"]] = relationship(
        "LibraryHoliday", back_populates="branch"
    )

    def __repr__(self) -> str:
        return f"<LibraryBranch {self.code}: {self.name}>"


class BookAuthor(Base):
    """Many-to-many association linking books and authors with explicit order."""

    __tablename__ = "book_authors"

    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), primary_key=True
    )
    author_id: Mapped[int] = mapped_column(
        ForeignKey("authors.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    author_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class Author(Base):
    """Catalog author entity."""

    __tablename__ = "authors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    normalized_name: Mapped[str] = mapped_column(
        String(160), unique=True, nullable=False, index=True
    )
    biography: Mapped[str | None] = mapped_column(Text, nullable=True)
    birth_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    books: Mapped[list["Book"]] = relationship(
        "Book",
        secondary="book_authors",
        back_populates="authors",
        order_by="Book.title",
    )

    @staticmethod
    def compute_normalized_name(name: str) -> str:
        cleaned = " ".join(name.strip().split())
        if not cleaned:
            raise ValueError("Author name must not be empty.")
        return cleaned.lower()

    def __repr__(self) -> str:
        return f"<Author {self.id}: {self.full_name}>"


class Book(Base):
    """Catalog title record (distinct from physical inventory copies)."""

    __tablename__ = "books"
    __table_args__ = (
        CheckConstraint(
            "publication_year >= 1450 AND publication_year <= 2100",
            name="valid_publication_year",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subtitle: Mapped[str | None] = mapped_column(String(255), nullable=True)
    isbn: Mapped[str | None] = mapped_column(
        String(13), unique=True, nullable=True, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    publisher: Mapped[str] = mapped_column(String(160), nullable=False)
    publication_year: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(
        String(50), nullable=False, default="English", index=True
    )
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    edition: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    authors: Mapped[list[Author]] = relationship(
        "Author",
        secondary="book_authors",
        back_populates="books",
        order_by="Author.full_name",
    )
    copies: Mapped[list["BookCopy"]] = relationship(
        "BookCopy", back_populates="book", order_by="BookCopy.accession_number"
    )
    reservations: Mapped[list["Reservation"]] = relationship(
        "Reservation", back_populates="book"
    )

    def set_validated_isbn(self, raw_isbn: str | None) -> None:
        parsed = ISBN.parse(raw_isbn)
        self.isbn = parsed.normalized if parsed else None

    @property
    def author_names(self) -> str:
        if not self.authors:
            return "Unknown Author"
        return ", ".join(a.full_name for a in self.authors)

    @property
    def available_copies_count(self) -> int:
        return sum(1 for c in self.copies if c.status == CopyStatus.AVAILABLE.value)

    def __repr__(self) -> str:
        return f"<Book {self.id}: {self.title} ({self.isbn or 'NO-ISBN'})>"


class BookCopy(Base):
    """Physical inventory copy of a catalog book owned by a specific branch."""

    __tablename__ = "book_copies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('AVAILABLE','ON_LOAN','RESERVED','LOST','DAMAGED','WITHDRAWN')",
            name="valid_copy_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    owning_branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    accession_number: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CopyStatus.AVAILABLE.value, index=True
    )
    acquisition_date: Mapped[date] = mapped_column(Date, nullable=False)
    condition_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    book: Mapped[Book] = relationship("Book", back_populates="copies")
    owning_branch: Mapped[LibraryBranch] = relationship(
        "LibraryBranch", back_populates="copies"
    )
    loans: Mapped[list["Loan"]] = relationship("Loan", back_populates="copy")

    @property
    def status_enum(self) -> CopyStatus:
        return CopyStatus(self.status)

    def assert_can_issue(self) -> None:
        CopyStateMachine.assert_can_issue(self.status_enum)

    def transition_to(
        self,
        new_status: CopyStatus,
        *,
        manual: bool = False,
        admin_override: bool = False,
        note: str | None = None,
    ) -> None:
        CopyStateMachine.validate_transition(
            self.status_enum,
            new_status,
            manual=manual,
            admin_override=admin_override,
        )
        self.status = new_status.value
        if note:
            existing = (self.condition_notes or "").strip()
            self.condition_notes = f"{existing}\n{note}".strip() if existing else note
        self.updated_at = utc_now()

    def __repr__(self) -> str:
        return f"<BookCopy {self.accession_number} [{self.status}]>"


class MembershipTier(Base):
    """Configurable borrowing and fine rules for a membership tier."""

    __tablename__ = "membership_tiers"
    __table_args__ = (
        CheckConstraint(
            "code IN ('BASIC','STANDARD','PREMIUM')",
            name="valid_tier_code",
        ),
        CheckConstraint("max_active_loans >= 0", name="nonneg_max_loans"),
        CheckConstraint("loan_duration_days >= 1", name="pos_loan_duration"),
        CheckConstraint("max_renewals >= 0", name="nonneg_max_renewals"),
        CheckConstraint("renewal_duration_days >= 1", name="pos_renewal_duration"),
        CheckConstraint("max_active_reservations >= 0", name="nonneg_max_reservations"),
        CheckConstraint("grace_period_days >= 0", name="nonneg_grace_days"),
        CheckConstraint("daily_fine_rate >= 0", name="nonneg_daily_rate"),
        CheckConstraint("max_fine_per_loan >= 0", name="nonneg_max_fine"),
        CheckConstraint("fine_blocking_threshold >= 0", name="nonneg_fine_threshold"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_active_loans: Mapped[int] = mapped_column(Integer, nullable=False)
    loan_duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    max_renewals: Mapped[int] = mapped_column(Integer, nullable=False)
    renewal_duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    max_active_reservations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3
    )
    grace_period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_fine_rate: Mapped[Decimal] = mapped_column(MoneyPaise, nullable=False)
    max_fine_per_loan: Mapped[Decimal] = mapped_column(MoneyPaise, nullable=False)
    fine_blocking_threshold: Mapped[Decimal] = mapped_column(MoneyPaise, nullable=False)
    borrowing_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    members: Mapped[list["Member"]] = relationship("Member", back_populates="tier")

    @property
    def tier_enum(self) -> TierCode:
        return TierCode(self.code)

    def __repr__(self) -> str:
        return f"<MembershipTier {self.code}>"


class Member(Base):
    """Registered public library member."""

    __tablename__ = "members"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE','SUSPENDED','EXPIRED','CANCELLED')",
            name="valid_member_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    member_code: Mapped[str] = mapped_column(
        String(30), unique=True, nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(
        String(254), unique=True, nullable=True, index=True
    )
    phone: Mapped[str | None] = mapped_column(String(25), nullable=True, index=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    home_branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    tier_id: Mapped[int] = mapped_column(
        ForeignKey("membership_tiers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MembershipStatus.ACTIVE.value, index=True
    )
    registration_date: Mapped[date] = mapped_column(Date, nullable=False)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    tier: Mapped[MembershipTier] = relationship("MembershipTier", back_populates="members")
    home_branch: Mapped[LibraryBranch | None] = relationship("LibraryBranch")
    loans: Mapped[list["Loan"]] = relationship(
        "Loan", back_populates="member", order_by="Loan.checked_out_at.desc()"
    )
    reservations: Mapped[list["Reservation"]] = relationship(
        "Reservation", back_populates="member", order_by="Reservation.created_at.desc()"
    )
    fines: Mapped[list["Fine"]] = relationship(
        "Fine", back_populates="member", order_by="Fine.assessed_at.desc()"
    )

    @property
    def status_enum(self) -> MembershipStatus:
        return MembershipStatus(self.status)

    def set_contact_details(self, email: str | None, phone: str | None) -> None:
        self.email = normalize_email(email)
        self.phone = normalize_phone(phone)

    def is_expired(self, as_of_date: date) -> bool:
        return self.expiry_date is not None and self.expiry_date < as_of_date

    def __repr__(self) -> str:
        return f"<Member {self.member_code}: {self.full_name} [{self.status}]>"


class User(Base):
    """Staff or administrator account with role-based permissions."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('ADMIN','LIBRARIAN','STAFF_READONLY')",
            name="valid_user_role",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(
        String(60), unique=True, nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(25), nullable=False, default=UserRole.LIBRARIAN.value
    )
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    branch: Mapped[LibraryBranch | None] = relationship("LibraryBranch")

    @property
    def role_enum(self) -> UserRole:
        return UserRole(self.role)

    def set_password(self, raw_password: str) -> None:
        if not raw_password or len(raw_password) < 8:
            raise ValueError("Password must be at least 8 characters long.")
        self.password_hash = generate_password_hash(raw_password)

    def verify_password(self, raw_password: str) -> bool:
        if not raw_password or not self.password_hash:
            return False
        return check_password_hash(self.password_hash, raw_password)

    def has_role(self, *allowed_roles: UserRole) -> bool:
        return self.is_active and self.role_enum in allowed_roles

    @property
    def is_admin(self) -> bool:
        return self.is_active and self.role_enum == UserRole.ADMIN

    @property
    def can_circulate(self) -> bool:
        return self.is_active and self.role_enum in (UserRole.ADMIN, UserRole.LIBRARIAN)

    def __repr__(self) -> str:
        return f"<User {self.username} [{self.role}]>"


class Loan(Base):
    """Circulation loan transaction for a specific physical copy."""

    __tablename__ = "loans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE','RETURNED','LOST')",
            name="valid_loan_status",
        ),
        CheckConstraint("renewal_count >= 0", name="nonneg_renewal_count"),
        Index(
            "uq_loans_one_active_per_copy",
            "copy_id",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_loans_status_due_date", "status", "due_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    loan_number: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, index=True
    )
    member_id: Mapped[int] = mapped_column(
        ForeignKey("members.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    copy_id: Mapped[int] = mapped_column(
        ForeignKey("book_copies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    issuing_branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    returned_branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=True
    )
    issued_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    checked_out_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    returned_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=LoanStatus.ACTIVE.value, index=True
    )
    renewal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    return_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_reference: Mapped[str | None] = mapped_column(
        String(80), unique=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    member: Mapped[Member] = relationship("Member", back_populates="loans")
    copy: Mapped[BookCopy] = relationship("BookCopy", back_populates="loans")
    issuing_branch: Mapped[LibraryBranch] = relationship(
        "LibraryBranch", foreign_keys=[issuing_branch_id]
    )
    returned_branch: Mapped[LibraryBranch | None] = relationship(
        "LibraryBranch", foreign_keys=[returned_branch_id]
    )
    issued_by_user: Mapped[User | None] = relationship("User")
    fine: Mapped["Fine | None"] = relationship(
        "Fine", back_populates="loan", uselist=False
    )

    @property
    def status_enum(self) -> LoanStatus:
        return LoanStatus(self.status)

    def is_overdue(self, as_of_date: date) -> bool:
        return self.status_enum == LoanStatus.ACTIVE and self.due_date < as_of_date

    def mark_returned(
        self,
        returned_at: datetime,
        *,
        returned_branch_id: int | None = None,
        notes: str | None = None,
    ) -> None:
        if self.status_enum != LoanStatus.ACTIVE:
            raise LoanAlreadyReturnedError(
                f"Loan {self.loan_number} is already closed with status {self.status}."
            )
        self.returned_at = returned_at
        self.returned_branch_id = returned_branch_id or self.issuing_branch_id
        self.status = LoanStatus.RETURNED.value
        if notes:
            self.return_notes = notes
        self.updated_at = utc_now()

    def mark_lost(self, recorded_at: datetime, *, notes: str | None = None) -> None:
        if self.status_enum != LoanStatus.ACTIVE:
            raise LoanAlreadyReturnedError(
                f"Loan {self.loan_number} is already closed with status {self.status}."
            )
        self.returned_at = recorded_at
        self.status = LoanStatus.LOST.value
        if notes:
            self.return_notes = notes
        self.updated_at = utc_now()

    def __repr__(self) -> str:
        return f"<Loan {self.loan_number} [{self.status}] due={self.due_date}>"


class Reservation(Base):
    """FIFO reservation queue entry for a catalog title."""

    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('WAITING','READY_FOR_PICKUP','FULFILLED','CANCELLED','EXPIRED')",
            name="valid_reservation_status",
        ),
        Index(
            "ix_reservations_queue_fifo",
            "book_id",
            "status",
            "created_at",
            "id",
        ),
        Index(
            "uq_reservations_one_active_per_member_book",
            "member_id",
            "book_id",
            unique=True,
            sqlite_where=text("status IN ('WAITING', 'READY_FOR_PICKUP')"),
        ),
        Index(
            "uq_reservations_one_ready_per_copy",
            "allocated_copy_id",
            unique=True,
            sqlite_where=text(
                "status = 'READY_FOR_PICKUP' AND allocated_copy_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reservation_number: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, index=True
    )
    book_id: Mapped[int] = mapped_column(
        ForeignKey("books.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    member_id: Mapped[int] = mapped_column(
        ForeignKey("members.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    preferred_branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    allocated_copy_id: Mapped[int | None] = mapped_column(
        ForeignKey("book_copies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(25), nullable=False, default=ReservationStatus.WAITING.value, index=True
    )
    allocated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    pickup_deadline_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True, index=True
    )
    fulfilled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_reference: Mapped[str | None] = mapped_column(
        String(80), unique=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    book: Mapped[Book] = relationship("Book", back_populates="reservations")
    member: Mapped[Member] = relationship("Member", back_populates="reservations")
    preferred_branch: Mapped[LibraryBranch | None] = relationship("LibraryBranch")
    allocated_copy: Mapped[BookCopy | None] = relationship("BookCopy")

    @property
    def status_enum(self) -> ReservationStatus:
        return ReservationStatus(self.status)

    def allocate_copy(
        self, copy_id: int, *, allocated_at: datetime, pickup_deadline_at: datetime
    ) -> None:
        ReservationStateMachine.validate_transition(
            self.status_enum, ReservationStatus.READY_FOR_PICKUP
        )
        self.allocated_copy_id = copy_id
        self.allocated_at = allocated_at
        self.pickup_deadline_at = pickup_deadline_at
        self.status = ReservationStatus.READY_FOR_PICKUP.value
        self.updated_at = utc_now()

    def mark_fulfilled(self, fulfilled_at: datetime) -> None:
        ReservationStateMachine.validate_transition(
            self.status_enum, ReservationStatus.FULFILLED
        )
        self.fulfilled_at = fulfilled_at
        self.status = ReservationStatus.FULFILLED.value
        self.updated_at = utc_now()

    def cancel(self, cancelled_at: datetime, *, note: str | None = None) -> None:
        ReservationStateMachine.validate_transition(
            self.status_enum, ReservationStatus.CANCELLED
        )
        self.cancelled_at = cancelled_at
        self.status = ReservationStatus.CANCELLED.value
        if note:
            self.notes = note
        self.updated_at = utc_now()

    def expire(self, expired_at: datetime) -> None:
        ReservationStateMachine.validate_transition(
            self.status_enum, ReservationStatus.EXPIRED
        )
        self.expired_at = expired_at
        self.status = ReservationStatus.EXPIRED.value
        self.updated_at = utc_now()

    def __repr__(self) -> str:
        return f"<Reservation {self.reservation_number} [{self.status}]>"


class Fine(Base):
    """Assessed fine ledger entry preserved permanently even when fully paid."""

    __tablename__ = "fines"
    __table_args__ = (
        CheckConstraint(
            "status IN ('UNPAID','PARTIALLY_PAID','PAID','WAIVED')",
            name="valid_fine_status",
        ),
        CheckConstraint("assessed_amount >= 0", name="nonneg_assessed_amount"),
        CheckConstraint("paid_amount >= 0", name="nonneg_paid_amount"),
        CheckConstraint("waived_amount >= 0", name="nonneg_waived_amount"),
        CheckConstraint(
            "paid_amount + waived_amount <= assessed_amount",
            name="settlement_not_exceeding_assessed",
        ),
        CheckConstraint("eligible_overdue_days >= 0", name="nonneg_overdue_days"),
        CheckConstraint("daily_rate_applied >= 0", name="nonneg_daily_rate_applied"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fine_number: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, index=True
    )
    loan_id: Mapped[int] = mapped_column(
        ForeignKey("loans.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
        index=True,
    )
    member_id: Mapped[int] = mapped_column(
        ForeignKey("members.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    assessed_amount: Mapped[Decimal] = mapped_column(MoneyPaise, nullable=False)
    paid_amount: Mapped[Decimal] = mapped_column(
        MoneyPaise, nullable=False, default=ZERO_MONEY
    )
    waived_amount: Mapped[Decimal] = mapped_column(
        MoneyPaise, nullable=False, default=ZERO_MONEY
    )
    eligible_overdue_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_rate_applied: Mapped[Decimal] = mapped_column(
        MoneyPaise, nullable=False, default=ZERO_MONEY
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FineStatus.UNPAID.value, index=True
    )
    reason: Mapped[str] = mapped_column(
        String(200), nullable=False, default="Overdue return fine"
    )
    assessed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    source_reference: Mapped[str | None] = mapped_column(
        String(80), unique=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    loan: Mapped[Loan] = relationship("Loan", back_populates="fine")
    member: Mapped[Member] = relationship("Member", back_populates="fines")
    payments: Mapped[list["FinePayment"]] = relationship(
        "FinePayment", back_populates="fine", order_by="FinePayment.paid_at.asc()"
    )

    @property
    def status_enum(self) -> FineStatus:
        return FineStatus(self.status)

    @property
    def outstanding_balance(self) -> Decimal:
        assessed = quantize_money(self.assessed_amount or ZERO_MONEY)
        paid = quantize_money(self.paid_amount or ZERO_MONEY)
        waived = quantize_money(self.waived_amount or ZERO_MONEY)
        rem = assessed - paid - waived
        return quantize_money(rem) if rem > ZERO_MONEY else ZERO_MONEY

    def apply_payment(self, amount: Decimal) -> None:
        payment_amount = quantize_money(amount)
        if payment_amount <= ZERO_MONEY:
            raise InvalidFinePaymentError("Payment amount must be strictly positive.")
        current_balance = self.outstanding_balance
        if current_balance <= ZERO_MONEY:
            raise InvalidFinePaymentError(
                f"Fine {self.fine_number} already has zero outstanding balance."
            )
        if payment_amount > current_balance:
            raise InvalidFinePaymentError(
                f"Payment amount INR {payment_amount} exceeds outstanding balance INR {current_balance}."
            )
        self.paid_amount = quantize_money(
            quantize_money(self.paid_amount or ZERO_MONEY) + payment_amount
        )
        if self.outstanding_balance == ZERO_MONEY:
            self.status = FineStatus.PAID.value
        else:
            self.status = FineStatus.PARTIALLY_PAID.value
        self.updated_at = utc_now()

    def waive_remaining(self) -> Decimal:
        rem = self.outstanding_balance
        if rem <= ZERO_MONEY:
            raise InvalidFinePaymentError(
                f"Fine {self.fine_number} has no remaining balance to waive."
            )
        self.waived_amount = quantize_money(
            quantize_money(self.waived_amount or ZERO_MONEY) + rem
        )
        paid = quantize_money(self.paid_amount or ZERO_MONEY)
        self.status = FineStatus.PAID.value if paid > ZERO_MONEY else FineStatus.WAIVED.value
        self.updated_at = utc_now()
        return rem

    def __repr__(self) -> str:
        return f"<Fine {self.fine_number} assessed={self.assessed_amount} balance={self.outstanding_balance}>"


class FinePayment(Base):
    """Immutable payment receipt applied toward an assessed Fine."""

    __tablename__ = "fine_payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="positive_payment_amount"),
        CheckConstraint(
            "payment_method IN ('CASH','UPI','CARD','BANK_TRANSFER')",
            name="valid_payment_method",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    receipt_number: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, index=True
    )
    fine_id: Mapped[int] = mapped_column(
        ForeignKey("fines.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    member_id: Mapped[int] = mapped_column(
        ForeignKey("members.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    received_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="SET NULL"), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(MoneyPaise, nullable=False)
    payment_method: Mapped[str] = mapped_column(
        String(25), nullable=False, default=PaymentMethod.CASH.value
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(80), unique=True, nullable=True
    )
    reference_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    paid_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now
    )

    fine: Mapped[Fine] = relationship("Fine", back_populates="payments")
    member: Mapped[Member] = relationship("Member")
    received_by_user: Mapped[User | None] = relationship("User")
    branch: Mapped[LibraryBranch | None] = relationship("LibraryBranch")

    def __repr__(self) -> str:
        return f"<FinePayment {self.receipt_number} INR {self.amount}>"


class LibraryHoliday(Base):
    """Configured holiday (network-wide if branch_id is NULL, else branch-specific)."""

    __tablename__ = "library_holidays"
    __table_args__ = (
        Index(
            "uq_holidays_network_date",
            "holiday_date",
            unique=True,
            sqlite_where=text("branch_id IS NULL"),
        ),
        Index(
            "uq_holidays_branch_date",
            "holiday_date",
            "branch_id",
            unique=True,
            sqlite_where=text("branch_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"), nullable=True, index=True
    )
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    branch: Mapped[LibraryBranch | None] = relationship(
        "LibraryBranch", back_populates="holidays"
    )

    def __repr__(self) -> str:
        scope = f"Branch#{self.branch_id}" if self.branch_id else "Network"
        return f"<LibraryHoliday {self.holiday_date} ({scope}): {self.name}>"


class AuditEvent(Base):
    """Append-only operational audit record committed inside the enclosing transaction."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="SET NULL"), nullable=True
    )
    summary: Mapped[str] = mapped_column(String(300), nullable=False)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now, index=True
    )

    actor_user: Mapped[User | None] = relationship("User")
    branch: Mapped[LibraryBranch | None] = relationship("LibraryBranch")

    @property
    def details(self) -> dict[str, Any]:
        if not self.details_json:
            return {}
        try:
            return json.loads(self.details_json)
        except json.JSONDecodeError:
            return {}

    def __repr__(self) -> str:
        return f"<AuditEvent {self.id} {self.event_type} {self.entity_type}:{self.entity_id}>"


class ImportBatch(Base):
    """Execution summary of a historical data import run."""

    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_key: Mapped[str] = mapped_column(
        String(80), unique=True, nullable=False, index=True
    )
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    total_records: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quarantined_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_skipped_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    initiated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    initiated_by_user: Mapped[User | None] = relationship("User")
    issues: Mapped[list["ImportIssue"]] = relationship(
        "ImportIssue", back_populates="batch", cascade="all, delete-orphan"
    )


class ImportIssue(Base):
    """Quarantined, rejected, duplicate, or errored historical source record."""

    __tablename__ = "import_issues"
    __table_args__ = (
        CheckConstraint(
            "disposition IN ('REJECTED','QUARANTINED','DUPLICATE_SKIPPED','ERROR')",
            name="valid_import_disposition",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_record_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    record_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    disposition: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    issue_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    message: Mapped[str] = mapped_column(String(400), nullable=False)
    raw_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utc_now
    )

    batch: Mapped[ImportBatch] = relationship("ImportBatch", back_populates="issues")

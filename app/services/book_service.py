"""Catalog, Author, and Physical Copy inventory management service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence

from sqlalchemy.orm import sessionmaker

from app.domain.enums import AuditEventType, CopyStatus, ReservationStatus
from app.domain.exceptions import (
    AuthorNotFoundError,
    BookNotFoundError,
    BranchNotFoundError,
    CatalogValidationError,
    CopyNotFoundError,
    DuplicateRecordError,
)
from app.domain.value_objects import ISBN, local_today, utc_now
from app.infrastructure.transactions import read_session, write_transaction
from app.models import Author, Book, BookCopy, Loan, Reservation
from app.repositories import (
    AuditRepository,
    AuthorRepository,
    BookRepository,
    BranchRepository,
    CopyRepository,
    LoanRepository,
    Page,
    ReservationRepository,
)
from app.services.reservation_service import ReservationService


@dataclass(frozen=True, slots=True)
class BranchCopyAvailability:
    branch_id: int
    branch_code: str
    branch_name: str
    total_copies: int
    available_copies: int
    on_loan_copies: int
    reserved_copies: int
    non_circulating_copies: int


@dataclass(frozen=True, slots=True)
class BookDetailView:
    book: Book
    branch_breakdown: list[BranchCopyAvailability]
    recent_loans: Sequence[Loan]
    waiting_reservations: Sequence[Reservation]


class BookService:
    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        reservation_service: ReservationService | None = None,
        library_timezone: str = "Asia/Kolkata",
    ) -> None:
        self.session_factory = session_factory
        self.library_timezone = library_timezone
        self.reservation_service = reservation_service or ReservationService(
            session_factory, library_timezone=library_timezone
        )

    @staticmethod
    def _validate_book_fields(
        *,
        title: str,
        publisher: str,
        publication_year: int,
        category: str,
        language: str,
    ) -> tuple[str, str, int, str, str]:
        clean_title = " ".join(title.strip().split())
        clean_pub = " ".join(publisher.strip().split())
        clean_cat = " ".join(category.strip().split())
        clean_lang = " ".join(language.strip().split()) or "English"

        if not clean_title or len(clean_title) > 255:
            raise CatalogValidationError("Book title is required (1-255 characters).")
        if not clean_pub or len(clean_pub) > 160:
            raise CatalogValidationError("Publisher is required (1-160 characters).")
        if not clean_cat or len(clean_cat) > 80:
            raise CatalogValidationError("Category is required (1-80 characters).")
        year = int(publication_year)
        if year < 1450 or year > 2100:
            raise CatalogValidationError(
                "Publication year must be between 1450 and 2100."
            )
        return clean_title, clean_pub, year, clean_cat, clean_lang

    @staticmethod
    def _parse_author_names(author_names: Sequence[str] | str) -> list[str]:
        if isinstance(author_names, str):
            raw_parts = [p.strip() for p in author_names.split(",")]
        else:
            raw_parts = [str(p).strip() for p in author_names]
        deduped: list[str] = []
        seen: set[str] = set()
        for item in raw_parts:
            cleaned = " ".join(item.split())
            if cleaned and cleaned.lower() not in seen:
                seen.add(cleaned.lower())
                deduped.append(cleaned)
        if not deduped:
            raise CatalogValidationError("At least one author name is required.")
        return deduped

    def create_book(
        self,
        *,
        title: str,
        publisher: str,
        publication_year: int,
        category: str,
        author_names: Sequence[str] | str,
        isbn: str | None = None,
        subtitle: str | None = None,
        description: str | None = None,
        language: str = "English",
        edition: str | None = None,
        actor_user_id: int | None = None,
    ) -> Book:
        clean_title, clean_pub, year, clean_cat, clean_lang = self._validate_book_fields(
            title=title,
            publisher=publisher,
            publication_year=publication_year,
            category=category,
            language=language,
        )
        parsed_isbn = ISBN.parse(isbn)
        parsed_authors = self._parse_author_names(author_names)
        clean_edition = edition.strip() if edition and edition.strip() else None

        with write_transaction(self.session_factory) as session:
            book_repo = BookRepository(session)
            author_repo = AuthorRepository(session)

            if parsed_isbn is not None:
                if book_repo.get_by_isbn(parsed_isbn.normalized) is not None:
                    raise DuplicateRecordError(
                        f"A catalog book with ISBN {parsed_isbn.normalized} already exists."
                    )
            else:
                if (
                    book_repo.find_exact_non_isbn_match(
                        title=clean_title,
                        publisher=clean_pub,
                        publication_year=year,
                        edition=clean_edition,
                    )
                    is not None
                ):
                    raise DuplicateRecordError(
                        "An identical non-ISBN catalog record (same title, publisher, year, and edition) already exists."
                    )

            book = Book(
                title=clean_title,
                subtitle=subtitle.strip() if subtitle and subtitle.strip() else None,
                isbn=parsed_isbn.normalized if parsed_isbn else None,
                description=description.strip() if description and description.strip() else None,
                publisher=clean_pub,
                publication_year=year,
                language=clean_lang,
                category=clean_cat,
                edition=clean_edition,
            )
            authors: list[Author] = []
            for name in parsed_authors:
                author, _ = author_repo.get_or_create(name)
                authors.append(author)
            book.authors = authors
            book_repo.add(book)

            AuditRepository(session).record(
                event_type=AuditEventType.BOOK_CREATED,
                entity_type="BOOK",
                entity_id=book.id,
                actor_user_id=actor_user_id,
                summary=f"Added catalog book '{book.title}' ({book.isbn or 'No ISBN'}).",
                details={"isbn": book.isbn, "authors": parsed_authors},
            )
            # Ensure eager relationships are populated before detachment
            return book_repo.get_by_id(book.id) or book

    def update_book(
        self,
        book_id: int,
        *,
        title: str,
        publisher: str,
        publication_year: int,
        category: str,
        author_names: Sequence[str] | str,
        isbn: str | None = None,
        subtitle: str | None = None,
        description: str | None = None,
        language: str = "English",
        edition: str | None = None,
        actor_user_id: int | None = None,
    ) -> Book:
        clean_title, clean_pub, year, clean_cat, clean_lang = self._validate_book_fields(
            title=title,
            publisher=publisher,
            publication_year=publication_year,
            category=category,
            language=language,
        )
        parsed_isbn = ISBN.parse(isbn)
        parsed_authors = self._parse_author_names(author_names)
        clean_edition = edition.strip() if edition and edition.strip() else None

        with write_transaction(self.session_factory) as session:
            book_repo = BookRepository(session)
            author_repo = AuthorRepository(session)
            book = book_repo.get_by_id(book_id)
            if book is None:
                raise BookNotFoundError(f"Book #{book_id} was not found.")

            if parsed_isbn is not None:
                existing = book_repo.get_by_isbn(parsed_isbn.normalized)
                if existing is not None and existing.id != book.id:
                    raise DuplicateRecordError(
                        f"ISBN {parsed_isbn.normalized} is already assigned to '{existing.title}'."
                    )
            else:
                existing_non_isbn = book_repo.find_exact_non_isbn_match(
                    title=clean_title,
                    publisher=clean_pub,
                    publication_year=year,
                    edition=clean_edition,
                )
                if existing_non_isbn is not None and existing_non_isbn.id != book.id:
                    raise DuplicateRecordError(
                        "An identical non-ISBN catalog record (same title, publisher, year, and edition) already exists."
                    )

            book.title = clean_title
            book.subtitle = subtitle.strip() if subtitle and subtitle.strip() else None
            book.isbn = parsed_isbn.normalized if parsed_isbn else None
            book.description = description.strip() if description and description.strip() else None
            book.publisher = clean_pub
            book.publication_year = year
            book.language = clean_lang
            book.category = clean_cat
            book.edition = clean_edition
            book.authors = [author_repo.get_or_create(name)[0] for name in parsed_authors]
            book.updated_at = utc_now()

            AuditRepository(session).record(
                event_type=AuditEventType.BOOK_UPDATED,
                entity_type="BOOK",
                entity_id=book.id,
                actor_user_id=actor_user_id,
                summary=f"Updated catalog book '{book.title}'.",
            )
            return book_repo.get_by_id(book.id) or book

    def get_book(self, book_id: int) -> Book:
        with read_session(self.session_factory) as session:
            book = BookRepository(session).get_by_id(book_id)
            if book is None:
                raise BookNotFoundError(f"Book #{book_id} was not found.")
            return book

    def get_book_detail(self, book_id: int) -> BookDetailView:
        with read_session(self.session_factory) as session:
            book = BookRepository(session).get_by_id(book_id)
            if book is None:
                raise BookNotFoundError(f"Book #{book_id} was not found.")
            branches = BranchRepository(session).list_all()
            breakdown: list[BranchCopyAvailability] = []
            for br in branches:
                br_copies = [c for c in book.copies if c.owning_branch_id == br.id]
                breakdown.append(
                    BranchCopyAvailability(
                        branch_id=br.id,
                        branch_code=br.code,
                        branch_name=br.name,
                        total_copies=len(br_copies),
                        available_copies=sum(
                            1 for c in br_copies if c.status == CopyStatus.AVAILABLE.value
                        ),
                        on_loan_copies=sum(
                            1 for c in br_copies if c.status == CopyStatus.ON_LOAN.value
                        ),
                        reserved_copies=sum(
                            1 for c in br_copies if c.status == CopyStatus.RESERVED.value
                        ),
                        non_circulating_copies=sum(
                            1
                            for c in br_copies
                            if c.status
                            in (
                                CopyStatus.LOST.value,
                                CopyStatus.DAMAGED.value,
                                CopyStatus.WITHDRAWN.value,
                            )
                        ),
                    )
                )
            recent_loans = LoanRepository(session).list_for_book(book_id, limit=25)
            waiting = ReservationRepository(session).list_waiting_fifo(book_id)
            return BookDetailView(
                book=book,
                branch_breakdown=breakdown,
                recent_loans=recent_loans,
                waiting_reservations=waiting,
            )

    def search_books(
        self,
        *,
        query: str | None = None,
        category: str | None = None,
        language: str | None = None,
        branch_id: int | None = None,
        available_only: bool = False,
        page: int = 1,
        per_page: int = 20,
    ) -> Page[Book]:
        with read_session(self.session_factory) as session:
            return BookRepository(session).search(
                query=query,
                category=category,
                language=language,
                branch_id=branch_id,
                available_only=available_only,
                page=page,
                per_page=per_page,
            )

    def list_categories(self) -> list[str]:
        with read_session(self.session_factory) as session:
            return BookRepository(session).list_categories()

    def list_languages(self) -> list[str]:
        with read_session(self.session_factory) as session:
            return BookRepository(session).list_languages()

    # --- Author management ---
    def create_author(
        self,
        *,
        full_name: str,
        biography: str | None = None,
        birth_year: int | None = None,
        actor_user_id: int | None = None,
    ) -> Author:
        cleaned = " ".join(full_name.strip().split())
        if not cleaned or len(cleaned) > 160:
            raise CatalogValidationError("Author full name is required (1-160 characters).")
        with write_transaction(self.session_factory) as session:
            repo = AuthorRepository(session)
            if repo.get_by_normalized_name(cleaned) is not None:
                raise DuplicateRecordError(f"Author '{cleaned}' already exists.")
            author = Author(
                full_name=cleaned,
                normalized_name=Author.compute_normalized_name(cleaned),
                biography=biography.strip() if biography and biography.strip() else None,
                birth_year=birth_year,
            )
            repo.add(author)
            AuditRepository(session).record(
                event_type=AuditEventType.AUTHOR_CREATED,
                entity_type="AUTHOR",
                entity_id=author.id,
                actor_user_id=actor_user_id,
                summary=f"Created author '{author.full_name}'.",
            )
            return author

    def update_author(
        self,
        author_id: int,
        *,
        full_name: str,
        biography: str | None = None,
        birth_year: int | None = None,
        actor_user_id: int | None = None,
    ) -> Author:
        cleaned = " ".join(full_name.strip().split())
        if not cleaned or len(cleaned) > 160:
            raise CatalogValidationError("Author full name is required (1-160 characters).")
        with write_transaction(self.session_factory) as session:
            repo = AuthorRepository(session)
            author = repo.get_by_id(author_id)
            if author is None:
                raise AuthorNotFoundError(f"Author #{author_id} was not found.")
            norm = Author.compute_normalized_name(cleaned)
            existing = repo.get_by_normalized_name(cleaned)
            if existing is not None and existing.id != author.id:
                raise DuplicateRecordError(f"Author '{cleaned}' already exists.")
            author.full_name = cleaned
            author.normalized_name = norm
            author.biography = biography.strip() if biography and biography.strip() else None
            author.birth_year = birth_year
            author.updated_at = utc_now()
            AuditRepository(session).record(
                event_type=AuditEventType.AUTHOR_UPDATED,
                entity_type="AUTHOR",
                entity_id=author.id,
                actor_user_id=actor_user_id,
                summary=f"Updated author '{author.full_name}'.",
            )
            return author

    def search_authors(
        self, query: str | None = None, *, page: int = 1, per_page: int = 25
    ) -> Page[Author]:
        with read_session(self.session_factory) as session:
            return AuthorRepository(session).search(query, page=page, per_page=per_page)

    def get_author(self, author_id: int) -> Author:
        with read_session(self.session_factory) as session:
            author = AuthorRepository(session).get_by_id(author_id)
            if author is None:
                raise AuthorNotFoundError(f"Author #{author_id} was not found.")
            return author

    # --- Physical copy management ---
    def add_copy(
        self,
        *,
        book_id: int,
        owning_branch_id: int,
        accession_number: str,
        acquisition_date: date | None = None,
        condition_notes: str | None = None,
        status: CopyStatus = CopyStatus.AVAILABLE,
        actor_user_id: int | None = None,
    ) -> BookCopy:
        clean_acc = accession_number.strip().upper()
        if not clean_acc or len(clean_acc) > 40:
            raise CatalogValidationError(
                "Inventory/accession number is required (1-40 characters)."
            )
        if status in (CopyStatus.ON_LOAN, CopyStatus.RESERVED):
            raise CatalogValidationError(
                "Newly added copies cannot start in ON_LOAN or RESERVED status."
            )
        acq_date = acquisition_date or local_today(self.library_timezone)

        with write_transaction(self.session_factory) as session:
            book = BookRepository(session).get_by_id(book_id)
            if book is None:
                raise BookNotFoundError(f"Book #{book_id} was not found.")
            branch = BranchRepository(session).get_by_id(owning_branch_id)
            if branch is None or not branch.is_active:
                raise BranchNotFoundError(
                    f"Active owning branch #{owning_branch_id} was not found."
                )
            copy_repo = CopyRepository(session)
            if copy_repo.get_by_accession(clean_acc) is not None:
                raise DuplicateRecordError(
                    f"Accession number '{clean_acc}' already exists in inventory."
                )

            copy = BookCopy(
                book_id=book.id,
                owning_branch_id=branch.id,
                accession_number=clean_acc,
                status=status.value,
                acquisition_date=acq_date,
                condition_notes=condition_notes.strip()
                if condition_notes and condition_notes.strip()
                else None,
            )
            copy_repo.add(copy)
            AuditRepository(session).record(
                event_type=AuditEventType.COPY_ADDED,
                entity_type="BOOK_COPY",
                entity_id=copy.accession_number,
                actor_user_id=actor_user_id,
                branch_id=branch.id,
                summary=f"Added physical copy '{copy.accession_number}' for '{book.title}' at {branch.code}.",
                details={"book_id": book.id, "branch_id": branch.id},
            )
            if status == CopyStatus.AVAILABLE:
                self.reservation_service.allocate_copy_to_next_waiting_in_session(
                    session,
                    copy,
                    now_utc=utc_now(),
                    actor_user_id=actor_user_id,
                )
            return copy_repo.get_by_id(copy.id) or copy

    def change_copy_status(
        self,
        copy_id: int,
        new_status: CopyStatus | str,
        *,
        note: str | None = None,
        admin_override: bool = False,
        actor_user_id: int | None = None,
    ) -> BookCopy:
        target_status = (
            new_status
            if isinstance(new_status, CopyStatus)
            else CopyStatus(new_status.strip().upper())
        )
        with write_transaction(self.session_factory) as session:
            copy_repo = CopyRepository(session)
            copy = copy_repo.get_by_id(copy_id)
            if copy is None:
                raise CopyNotFoundError(f"Physical copy #{copy_id} was not found.")

            # Extra safeguard: verify no active loan exists even if status drifted
            active_loan = LoanRepository(session).get_active_loan_for_copy(copy.id)
            if active_loan is not None:
                copy.status = CopyStatus.ON_LOAN.value

            previous_status = copy.status_enum

            # If a RESERVED copy is found damaged/lost/withdrawn on the hold shelf,
            # return its READY_FOR_PICKUP reservation back to WAITING (preserving original FIFO created_at)
            if previous_status == CopyStatus.RESERVED and target_status in (
                CopyStatus.DAMAGED,
                CopyStatus.LOST,
                CopyStatus.WITHDRAWN,
            ):
                held_res = ReservationRepository(session).get_ready_for_copy(copy.id)
                if held_res is not None:
                    held_res.status = ReservationStatus.WAITING.value
                    held_res.allocated_copy_id = None
                    held_res.allocated_at = None
                    held_res.pickup_deadline_at = None
                    held_res.updated_at = utc_now()

            copy.transition_to(
                target_status,
                manual=True,
                admin_override=admin_override,
                note=note,
            )

            AuditRepository(session).record(
                event_type=AuditEventType.COPY_STATUS_CHANGED,
                entity_type="BOOK_COPY",
                entity_id=copy.accession_number,
                actor_user_id=actor_user_id,
                branch_id=copy.owning_branch_id,
                summary=f"Changed copy {copy.accession_number} status from {previous_status.value} to {target_status.value}.",
                details={
                    "previous_status": previous_status.value,
                    "new_status": target_status.value,
                    "note": note,
                },
            )
            if target_status == CopyStatus.AVAILABLE and previous_status != CopyStatus.AVAILABLE:
                self.reservation_service.allocate_copy_to_next_waiting_in_session(
                    session,
                    copy,
                    now_utc=utc_now(),
                    actor_user_id=actor_user_id,
                )
            return copy_repo.get_by_id(copy.id) or copy

    def get_copy(self, copy_id: int) -> BookCopy:
        with read_session(self.session_factory) as session:
            copy = CopyRepository(session).get_by_id(copy_id)
            if copy is None:
                raise CopyNotFoundError(f"Physical copy #{copy_id} was not found.")
            return copy

    def search_copies(
        self,
        *,
        query: str | None = None,
        branch_id: int | None = None,
        status: str | None = None,
        book_id: int | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> Page[BookCopy]:
        with read_session(self.session_factory) as session:
            return CopyRepository(session).search(
                query=query,
                branch_id=branch_id,
                status=status,
                book_id=book_id,
                page=page,
                per_page=per_page,
            )

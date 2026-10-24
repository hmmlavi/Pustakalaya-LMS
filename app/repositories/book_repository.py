"""Repositories for Book and Author catalog persistence and search."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.domain.enums import CopyStatus
from app.models import Author, Book, BookAuthor, BookCopy
from app.repositories.base import Page, escape_like, paginate_statement


class AuthorRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, author_id: int) -> Author | None:
        stmt = (
            select(Author)
            .options(selectinload(Author.books))
            .where(Author.id == author_id)
        )
        return self.session.scalar(stmt)

    def get_by_normalized_name(self, name: str) -> Author | None:
        norm = Author.compute_normalized_name(name)
        stmt = select(Author).where(Author.normalized_name == norm)
        return self.session.scalar(stmt)

    def get_or_create(
        self,
        full_name: str,
        *,
        biography: str | None = None,
        birth_year: int | None = None,
    ) -> tuple[Author, bool]:
        cleaned = " ".join(full_name.strip().split())
        existing = self.get_by_normalized_name(cleaned)
        if existing is not None:
            return existing, False
        author = Author(
            full_name=cleaned,
            normalized_name=Author.compute_normalized_name(cleaned),
            biography=biography,
            birth_year=birth_year,
        )
        self.session.add(author)
        self.session.flush()
        return author, True

    def search(
        self, query: str | None = None, *, page: int = 1, per_page: int = 25
    ) -> Page[Author]:
        stmt = (
            select(Author)
            .options(selectinload(Author.books))
            .order_by(Author.full_name.asc())
        )
        if query and query.strip():
            like_expr = f"%{escape_like(query.strip().lower())}%"
            stmt = stmt.where(
                func.lower(Author.full_name).like(like_expr, escape="\\")
            )
        return paginate_statement(self.session, stmt, page=page, per_page=per_page)

    def list_all(self) -> Sequence[Author]:
        return list(
            self.session.scalars(select(Author).order_by(Author.full_name.asc())).all()
        )

    def add(self, author: Author) -> Author:
        self.session.add(author)
        self.session.flush()
        return author


class BookRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, book_id: int) -> Book | None:
        stmt = (
            select(Book)
            .options(
                selectinload(Book.authors),
                selectinload(Book.copies).joinedload(BookCopy.owning_branch),
            )
            .where(Book.id == book_id)
        )
        return self.session.scalar(stmt)

    def get_by_isbn(self, normalized_isbn: str) -> Book | None:
        stmt = (
            select(Book)
            .options(selectinload(Book.authors))
            .where(Book.isbn == normalized_isbn)
        )
        return self.session.scalar(stmt)

    def find_exact_non_isbn_match(
        self,
        *,
        title: str,
        publisher: str,
        publication_year: int,
        edition: str | None,
    ) -> Book | None:
        stmt = select(Book).where(
            func.lower(Book.title) == title.strip().lower(),
            func.lower(Book.publisher) == publisher.strip().lower(),
            Book.publication_year == publication_year,
        )
        if edition and edition.strip():
            stmt = stmt.where(func.lower(Book.edition) == edition.strip().lower())
        else:
            stmt = stmt.where(or_(Book.edition.is_(None), Book.edition == ""))
        return self.session.scalar(stmt)

    def search(
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
        """Database-backed paginated search across title, author name, and ISBN."""
        stmt = select(Book).options(
            selectinload(Book.authors),
            selectinload(Book.copies).joinedload(BookCopy.owning_branch),
        )

        if query and query.strip():
            raw_q = query.strip()
            cleaned_isbn_q = raw_q.replace("-", "").replace(" ", "").upper()
            like_q = f"%{escape_like(raw_q.lower())}%"
            isbn_like_q = f"%{escape_like(cleaned_isbn_q)}%"

            author_match_subq = (
                select(BookAuthor.book_id)
                .join(Author, Author.id == BookAuthor.author_id)
                .where(func.lower(Author.full_name).like(like_q, escape="\\"))
            )
            stmt = stmt.where(
                or_(
                    func.lower(Book.title).like(like_q, escape="\\"),
                    func.lower(func.coalesce(Book.subtitle, "")).like(
                        like_q, escape="\\"
                    ),
                    Book.isbn.like(isbn_like_q, escape="\\"),
                    Book.id.in_(author_match_subq),
                )
            )

        if category and category.strip():
            stmt = stmt.where(Book.category == category.strip())

        if language and language.strip():
            stmt = stmt.where(Book.language == language.strip())

        if branch_id is not None or available_only:
            copy_subq = select(BookCopy.book_id).where(
                BookCopy.status != CopyStatus.WITHDRAWN.value
            )
            if branch_id is not None:
                copy_subq = copy_subq.where(BookCopy.owning_branch_id == branch_id)
            if available_only:
                copy_subq = copy_subq.where(
                    BookCopy.status == CopyStatus.AVAILABLE.value
                )
            stmt = stmt.where(Book.id.in_(copy_subq))

        stmt = stmt.order_by(Book.title.asc(), Book.id.asc())
        return paginate_statement(self.session, stmt, page=page, per_page=per_page)

    def list_categories(self) -> list[str]:
        stmt = select(Book.category).distinct().order_by(Book.category.asc())
        return [c for c in self.session.scalars(stmt).all() if c]

    def list_languages(self) -> list[str]:
        stmt = select(Book.language).distinct().order_by(Book.language.asc())
        return [lang for lang in self.session.scalars(stmt).all() if lang]

    def count_all(self) -> int:
        return int(self.session.scalar(select(func.count(Book.id))) or 0)

    def add(self, book: Book) -> Book:
        self.session.add(book)
        self.session.flush()
        return book

"""Book catalog and author management routes (Module B)."""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.domain.enums import UserRole
from app.domain.exceptions import DomainError, NotFoundError
from app.web.auth_utils import get_current_user, login_required, roles_required
from app.web.forms import AuthorForm, BookCopyForm, BookForm
from app.web.routes.helpers import branch_choices, flash_form_errors, get_services

books_bp = Blueprint("books", __name__, url_prefix="/books")


@books_bp.route("")
@login_required
def list_books():
    book_svc = get_services()["books"]
    admin_svc = get_services()["admin"]

    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    language = request.args.get("language", "").strip()
    branch_id_raw = request.args.get("branch_id", "").strip()
    branch_id = int(branch_id_raw) if branch_id_raw.isdigit() and int(branch_id_raw) > 0 else None
    available_only = request.args.get("available_only") == "1"
    page = max(1, request.args.get("page", 1, type=int))

    results = book_svc.search_books(
        query=query or None,
        category=category or None,
        language=language or None,
        branch_id=branch_id,
        available_only=available_only,
        page=page,
        per_page=20,
    )

    return render_template(
        "books/list.html",
        results=results,
        query=query,
        selected_category=category,
        selected_language=language,
        selected_branch_id=branch_id,
        available_only=available_only,
        categories=book_svc.list_categories(),
        languages=book_svc.list_languages(),
        branches=admin_svc.list_branches(),
        active_nav="books",
    )


@books_bp.route("/new", methods=["GET", "POST"])
@roles_required(UserRole.ADMIN)
def create_book():
    form = BookForm()
    if form.validate_on_submit():
        user = get_current_user()
        try:
            book = get_services()["books"].create_book(
                title=form.title.data or "",
                subtitle=form.subtitle.data,
                isbn=form.isbn.data,
                author_names=form.author_names.data or "",
                publisher=form.publisher.data or "",
                publication_year=int(form.publication_year.data or 2020),
                category=form.category.data or "",
                language=form.language.data or "English",
                edition=form.edition.data,
                description=form.description.data,
                actor_user_id=user.id if user else None,
            )
            flash(f"Added catalog book '{book.title}'.", "success")
            return redirect(url_for("books.detail", book_id=book.id))
        except (DomainError, ValueError) as exc:
            flash(str(exc), "danger")
    elif request.method == "POST":
        flash_form_errors(form)

    return render_template(
        "books/form.html",
        form=form,
        is_edit=False,
        book=None,
        active_nav="books",
    )


@books_bp.route("/<int:book_id>")
@login_required
def detail(book_id: int):
    try:
        detail_view = get_services()["books"].get_book_detail(book_id)
    except NotFoundError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("books.list_books"))

    copy_form = BookCopyForm(book_id=book_id)
    copy_form.owning_branch_id.choices = branch_choices()

    return render_template(
        "books/detail.html",
        detail=detail_view,
        book=detail_view.book,
        copy_form=copy_form,
        active_nav="books",
    )


@books_bp.route("/<int:book_id>/edit", methods=["GET", "POST"])
@roles_required(UserRole.ADMIN)
def edit_book(book_id: int):
    book_svc = get_services()["books"]
    try:
        book = book_svc.get_book(book_id)
    except NotFoundError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("books.list_books"))

    if request.method == "GET":
        form = BookForm(
            title=book.title,
            subtitle=book.subtitle,
            isbn=book.isbn,
            author_names=", ".join(a.full_name for a in book.authors),
            publisher=book.publisher,
            publication_year=book.publication_year,
            category=book.category,
            language=book.language,
            edition=book.edition,
            description=book.description,
        )
    else:
        form = BookForm()

    if form.validate_on_submit():
        user = get_current_user()
        try:
            updated = book_svc.update_book(
                book_id,
                title=form.title.data or "",
                subtitle=form.subtitle.data,
                isbn=form.isbn.data,
                author_names=form.author_names.data or "",
                publisher=form.publisher.data or "",
                publication_year=int(form.publication_year.data or 2020),
                category=form.category.data or "",
                language=form.language.data or "English",
                edition=form.edition.data,
                description=form.description.data,
                actor_user_id=user.id if user else None,
            )
            flash(f"Updated catalog book '{updated.title}'.", "success")
            return redirect(url_for("books.detail", book_id=updated.id))
        except (DomainError, ValueError) as exc:
            flash(str(exc), "danger")
    elif request.method == "POST":
        flash_form_errors(form)

    return render_template(
        "books/form.html",
        form=form,
        is_edit=True,
        book=book,
        active_nav="books",
    )


@books_bp.route("/authors", methods=["GET", "POST"])
@login_required
def authors():
    book_svc = get_services()["books"]
    form = AuthorForm()

    if request.method == "POST":
        user = get_current_user()
        if user is None or not user.has_role(UserRole.ADMIN):
            abort(403)
        if form.validate_on_submit():
            try:
                author = book_svc.create_author(
                    full_name=form.full_name.data or "",
                    birth_year=form.birth_year.data,
                    biography=form.biography.data,
                    actor_user_id=user.id,
                )
                flash(f"Added author '{author.full_name}'.", "success")
                return redirect(url_for("books.authors"))
            except (DomainError, ValueError) as exc:
                flash(str(exc), "danger")
        else:
            flash_form_errors(form)

    query = request.args.get("q", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    results = book_svc.search_authors(query=query or None, page=page, per_page=25)
    return render_template(
        "books/authors.html",
        results=results,
        query=query,
        form=form,
        active_nav="books",
    )


@books_bp.route("/authors/<int:author_id>/edit", methods=["GET", "POST"])
@roles_required(UserRole.ADMIN)
def edit_author(author_id: int):
    book_svc = get_services()["books"]
    try:
        author = book_svc.get_author(author_id)
    except NotFoundError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("books.authors"))

    form = (
        AuthorForm(
            full_name=author.full_name,
            birth_year=author.birth_year,
            biography=author.biography,
        )
        if request.method == "GET"
        else AuthorForm()
    )
    if form.validate_on_submit():
        user = get_current_user()
        try:
            book_svc.update_author(
                author_id,
                full_name=form.full_name.data or "",
                birth_year=form.birth_year.data,
                biography=form.biography.data,
                actor_user_id=user.id if user else None,
            )
            flash(f"Updated author '{form.full_name.data}'.", "success")
            return redirect(url_for("books.authors"))
        except (DomainError, ValueError) as exc:
            flash(str(exc), "danger")
    elif request.method == "POST":
        flash_form_errors(form)

    return render_template(
        "books/author_edit.html",
        author=author,
        form=form,
        active_nav="books",
    )

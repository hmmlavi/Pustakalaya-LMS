"""Physical BookCopy inventory routes (Navigation section 3: Book Copies)."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.domain.enums import CopyStatus, UserRole
from app.domain.exceptions import DomainError, NotFoundError
from app.web.auth_utils import get_current_user, login_required, roles_required
from app.web.forms import BookCopyForm, CopyStatusForm
from app.web.routes.helpers import (
    branch_choices,
    flash_form_errors,
    get_services,
    safe_referrer_or,
)

copies_bp = Blueprint("copies", __name__, url_prefix="/copies")


@copies_bp.route("")
@login_required
def list_copies():
    book_svc = get_services()["books"]
    admin_svc = get_services()["admin"]

    query = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    branch_id_raw = request.args.get("branch_id", "").strip()
    branch_id = (
        int(branch_id_raw)
        if branch_id_raw.isdigit() and int(branch_id_raw) > 0
        else None
    )
    page = max(1, request.args.get("page", 1, type=int))

    results = book_svc.search_copies(
        query=query or None,
        branch_id=branch_id,
        status=status or None,
        page=page,
        per_page=25,
    )
    add_form = BookCopyForm()
    add_form.owning_branch_id.choices = branch_choices()
    status_form = CopyStatusForm()

    return render_template(
        "copies/list.html",
        results=results,
        query=query,
        selected_status=status,
        selected_branch_id=branch_id,
        branches=admin_svc.list_branches(),
        statuses=[s.value for s in CopyStatus],
        add_form=add_form,
        status_form=status_form,
        active_nav="copies",
    )


@copies_bp.route("/new", methods=["POST"])
@roles_required(UserRole.ADMIN)
def add_copy():
    form = BookCopyForm()
    form.owning_branch_id.choices = branch_choices(active_only=False)
    if form.validate_on_submit():
        user = get_current_user()
        try:
            copy = get_services()["books"].add_copy(
                book_id=int(form.book_id.data or 0),
                owning_branch_id=int(form.owning_branch_id.data or 0),
                accession_number=form.accession_number.data or "",
                acquisition_date=form.acquisition_date.data,
                status=CopyStatus(form.status.data or "AVAILABLE"),
                condition_notes=form.condition_notes.data,
                actor_user_id=user.id if user else None,
            )
            flash(
                f"Added physical copy '{copy.accession_number}' at {copy.owning_branch.code}.",
                "success",
            )
            return redirect(url_for("books.detail", book_id=copy.book_id))
        except (DomainError, ValueError) as exc:
            flash(str(exc), "danger")
    else:
        flash_form_errors(form)

    return redirect(url_for("copies.list_copies"))


@copies_bp.route("/<int:copy_id>/status", methods=["POST"])
@roles_required(UserRole.ADMIN)
def change_status(copy_id: int):
    form = CopyStatusForm()
    if form.validate_on_submit():
        user = get_current_user()
        try:
            copy = get_services()["books"].change_copy_status(
                copy_id,
                form.new_status.data or "",
                note=form.note.data,
                actor_user_id=user.id if user else None,
            )
            flash(
                f"Updated copy {copy.accession_number} status to {copy.status}.",
                "success",
            )
        except (DomainError, NotFoundError, ValueError) as exc:
            flash(str(exc), "danger")
    else:
        flash_form_errors(form)

    return redirect(safe_referrer_or(url_for("copies.list_copies")))

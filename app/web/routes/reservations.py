"""Reservation queue routes (Module E)."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.domain.enums import ReservationStatus, UserRole
from app.domain.exceptions import DomainError
from app.web.auth_utils import get_current_user, login_required, roles_required
from app.web.forms import (
    ActionCSRFForm,
    ReservationCancelForm,
    ReservationCreateForm,
)
from app.web.routes.helpers import (
    branch_choices,
    flash_form_errors,
    get_services,
    safe_referrer_or,
)

reservations_bp = Blueprint("reservations", __name__, url_prefix="/reservations")


@reservations_bp.route("")
@login_required
def list_reservations():
    res_svc = get_services()["reservations"]
    admin_svc = get_services()["admin"]

    create_form = ReservationCreateForm(
        book_id=request.args.get("book_id", type=int),
        member_identifier=request.args.get("member", ""),
    )
    create_form.preferred_branch_id.choices = branch_choices(
        include_any_label="— Any Branch across Network —"
    )
    cancel_form = ReservationCancelForm()
    csrf_form = ActionCSRFForm()

    query = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    branch_id_raw = request.args.get("branch_id", "").strip()
    branch_id = (
        int(branch_id_raw)
        if branch_id_raw.isdigit() and int(branch_id_raw) > 0
        else None
    )
    page = max(1, request.args.get("page", 1, type=int))

    results, queue_positions = res_svc.search_reservations(
        query=query or None,
        status=status or None,
        branch_id=branch_id,
        page=page,
        per_page=25,
    )

    return render_template(
        "reservations/list.html",
        results=results,
        queue_positions=queue_positions,
        create_form=create_form,
        cancel_form=cancel_form,
        csrf_form=csrf_form,
        query=query,
        selected_status=status,
        selected_branch_id=branch_id,
        statuses=[s.value for s in ReservationStatus],
        branches=admin_svc.list_branches(),
        active_nav="reservations",
    )


@reservations_bp.route("/new", methods=["POST"])
@roles_required(UserRole.ADMIN, UserRole.LIBRARIAN)
def create():
    form = ReservationCreateForm()
    form.preferred_branch_id.choices = branch_choices(
        include_any_label="— Any Branch —", active_only=False
    )
    if form.validate_on_submit():
        user = get_current_user()
        pref_branch = (
            form.preferred_branch_id.data
            if form.preferred_branch_id.data and form.preferred_branch_id.data > 0
            else None
        )
        try:
            res = get_services()["reservations"].create_reservation(
                member_identifier=(form.member_identifier.data or "").strip(),
                book_id=int(form.book_id.data or 0),
                preferred_branch_id=pref_branch,
                notes=form.notes.data,
                actor_user_id=user.id if user else None,
            )
            pos = get_services()["reservations"].get_queue_position(res.id)
            flash(
                f"Created reservation {res.reservation_number} for '{res.book.title}' "
                f"(FIFO queue position #{pos}).",
                "success",
            )
        except DomainError as exc:
            flash(f"Reservation rejected: {exc.message}", "danger")
    else:
        flash_form_errors(form)

    return redirect(url_for("reservations.list_reservations"))


@reservations_bp.route("/<int:reservation_id>/cancel", methods=["POST"])
@roles_required(UserRole.ADMIN, UserRole.LIBRARIAN)
def cancel(reservation_id: int):
    form = ReservationCancelForm()
    if form.validate_on_submit():
        user = get_current_user()
        try:
            res = get_services()["reservations"].cancel_reservation(
                reservation_id,
                reason=form.reason.data,
                actor_user_id=user.id if user else None,
            )
            flash(
                f"Cancelled reservation {res.reservation_number}. Any held copy was automatically released to the next eligible queue member.",
                "info",
            )
        except DomainError as exc:
            flash(f"Cancellation failed: {exc.message}", "danger")
    else:
        flash_form_errors(form)

    return redirect(safe_referrer_or(url_for("reservations.list_reservations")))


@reservations_bp.route("/process-expired", methods=["POST"])
@roles_required(UserRole.ADMIN, UserRole.LIBRARIAN)
def process_expired():
    form = ActionCSRFForm()
    if form.validate_on_submit():
        user = get_current_user()
        expired = get_services()["reservations"].process_expired_reservations(
            actor_user_id=user.id if user else None
        )
        flash(
            f"Processed reservation expirations: {len(expired)} expired hold(s) released.",
            "info",
        )
    else:
        flash_form_errors(form)
    return redirect(url_for("reservations.list_reservations"))

"""Fine ledger and payment routes (Module F)."""

from __future__ import annotations

import uuid

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.domain.enums import FineStatus, PaymentMethod, UserRole
from app.domain.exceptions import DomainError, NotFoundError
from app.web.auth_utils import get_current_user, login_required, roles_required
from app.web.forms import FinePaymentForm, FineWaiveForm
from app.web.routes.helpers import branch_choices, flash_form_errors, get_services

fines_bp = Blueprint("fines", __name__, url_prefix="/fines")


@fines_bp.route("")
@login_required
def list_fines():
    fine_svc = get_services()["fines"]
    admin_svc = get_services()["admin"]

    query = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    branch_id_raw = request.args.get("branch_id", "").strip()
    branch_id = (
        int(branch_id_raw)
        if branch_id_raw.isdigit() and int(branch_id_raw) > 0
        else None
    )
    outstanding_only = request.args.get("outstanding_only") == "1"
    page = max(1, request.args.get("page", 1, type=int))

    results = fine_svc.search_fines(
        query=query or None,
        status=status or None,
        branch_id=branch_id,
        outstanding_only=outstanding_only,
        page=page,
        per_page=25,
    )

    return render_template(
        "fines/list.html",
        results=results,
        query=query,
        selected_status=status,
        selected_branch_id=branch_id,
        outstanding_only=outstanding_only,
        statuses=[s.value for s in FineStatus],
        branches=admin_svc.list_branches(),
        active_nav="fines",
    )


@fines_bp.route("/payments")
@login_required
def payments_history():
    fine_svc = get_services()["fines"]
    admin_svc = get_services()["admin"]

    query = request.args.get("q", "").strip()
    method = request.args.get("method", "").strip()
    branch_id_raw = request.args.get("branch_id", "").strip()
    branch_id = (
        int(branch_id_raw)
        if branch_id_raw.isdigit() and int(branch_id_raw) > 0
        else None
    )
    page = max(1, request.args.get("page", 1, type=int))

    results = fine_svc.search_payments(
        query=query or None,
        branch_id=branch_id,
        payment_method=method or None,
        page=page,
        per_page=25,
    )
    return render_template(
        "fines/payments.html",
        results=results,
        query=query,
        selected_method=method,
        selected_branch_id=branch_id,
        methods=[m.value for m in PaymentMethod],
        branches=admin_svc.list_branches(),
        active_nav="fines",
    )


@fines_bp.route("/<int:fine_id>")
@login_required
def detail(fine_id: int):
    try:
        fine = get_services()["fines"].get_fine(fine_id)
    except NotFoundError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("fines.list_fines"))

    pay_form = FinePaymentForm(
        amount=fine.outstanding_balance,
        idempotency_key=f"WEB-PMT-{fine.id}-{uuid.uuid4().hex[:12]}",
    )
    pay_form.branch_id.choices = branch_choices(
        include_any_label="— Loan's Issuing Branch —"
    )
    waive_form = FineWaiveForm()

    return render_template(
        "fines/detail.html",
        fine=fine,
        pay_form=pay_form,
        waive_form=waive_form,
        active_nav="fines",
    )


@fines_bp.route("/<int:fine_id>/pay", methods=["POST"])
@roles_required(UserRole.ADMIN, UserRole.LIBRARIAN)
def pay(fine_id: int):
    form = FinePaymentForm()
    form.branch_id.choices = branch_choices(
        include_any_label="— Default —", active_only=False
    )
    if form.validate_on_submit():
        user = get_current_user()
        branch_id = (
            form.branch_id.data
            if form.branch_id.data and form.branch_id.data > 0
            else None
        )
        try:
            receipt = get_services()["fines"].record_payment(
                fine_id,
                amount=form.amount.data,
                payment_method=form.payment_method.data or "CASH",
                idempotency_key=form.idempotency_key.data,
                reference_note=form.reference_note.data,
                branch_id=branch_id,
                actor_user_id=user.id if user else None,
            )
            flash(
                f"Recorded payment receipt {receipt.receipt_number} for INR {receipt.amount:.2f}.",
                "success",
            )
        except DomainError as exc:
            flash(f"Payment rejected: {exc.message}", "danger")
    else:
        flash_form_errors(form)

    return redirect(url_for("fines.detail", fine_id=fine_id))


@fines_bp.route("/<int:fine_id>/waive", methods=["POST"])
@roles_required(UserRole.ADMIN)
def waive(fine_id: int):
    form = FineWaiveForm()
    if form.validate_on_submit():
        user = get_current_user()
        assert user is not None
        try:
            fine = get_services()["fines"].waive_fine(
                fine_id,
                reason=form.reason.data or "",
                actor_user_id=user.id,
            )
            flash(
                f"Waived remaining balance on fine {fine.fine_number} (status={fine.status}).",
                "info",
            )
        except DomainError as exc:
            flash(f"Waiver rejected: {exc.message}", "danger")
    else:
        flash_form_errors(form)

    return redirect(url_for("fines.detail", fine_id=fine_id))

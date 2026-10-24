"""Circulation desk routes for issuing, returning, renewing, and lost-copy reporting (Module D)."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.domain.enums import LoanStatus, UserRole
from app.domain.exceptions import DomainError
from app.domain.value_objects import ZERO_MONEY, local_today
from app.web.auth_utils import get_current_user, login_required, roles_required
from app.web.forms import (
    ActionCSRFForm,
    IssueCopyForm,
    ReportLostForm,
    ReturnCopyForm,
)
from app.web.routes.helpers import (
    branch_choices,
    flash_form_errors,
    get_services,
    safe_referrer_or,
)

circulation_bp = Blueprint("circulation", __name__, url_prefix="/circulation")


@circulation_bp.route("")
@login_required
def index():
    circ_svc = get_services()["circulation"]
    admin_svc = get_services()["admin"]

    issue_form = IssueCopyForm(
        member_identifier=request.args.get("member", ""),
        copy_identifier=request.args.get("copy", ""),
    )
    issue_form.issuing_branch_id.choices = branch_choices(
        include_any_label="— Auto-detect from Copy's Owning Branch —"
    )

    return_form = ReturnCopyForm(identifier=request.args.get("return_ref", ""))
    return_form.returned_branch_id.choices = branch_choices(
        include_any_label="— Issuing Branch (Default) —"
    )

    csrf_form = ActionCSRFForm()
    lost_form = ReportLostForm()

    query = request.args.get("q", "").strip()
    status = request.args.get("status", "ACTIVE").strip()
    if status == "ALL":
        status_filter = None
    else:
        status_filter = status or None

    branch_id_raw = request.args.get("branch_id", "").strip()
    branch_id = (
        int(branch_id_raw)
        if branch_id_raw.isdigit() and int(branch_id_raw) > 0
        else None
    )
    overdue_only = request.args.get("overdue_only") == "1"
    page = max(1, request.args.get("page", 1, type=int))

    loans_page = circ_svc.search_loans(
        query=query or None,
        status=status_filter,
        branch_id=branch_id,
        overdue_only=overdue_only,
        page=page,
        per_page=25,
    )

    return render_template(
        "circulation/index.html",
        issue_form=issue_form,
        return_form=return_form,
        csrf_form=csrf_form,
        lost_form=lost_form,
        loans_page=loans_page,
        query=query,
        selected_status=status,
        selected_branch_id=branch_id,
        overdue_only=overdue_only,
        statuses=["ACTIVE", "RETURNED", "LOST", "ALL"],
        branches=admin_svc.list_branches(),
        today=local_today(),
        active_nav="circulation",
    )


@circulation_bp.route("/issue", methods=["POST"])
@roles_required(UserRole.ADMIN, UserRole.LIBRARIAN)
def issue():
    form = IssueCopyForm()
    form.issuing_branch_id.choices = branch_choices(
        include_any_label="— Auto-detect —", active_only=False
    )
    if form.validate_on_submit():
        user = get_current_user()
        branch_id = (
            form.issuing_branch_id.data
            if form.issuing_branch_id.data and form.issuing_branch_id.data > 0
            else None
        )
        try:
            loan = get_services()["circulation"].issue_copy(
                member_identifier=(form.member_identifier.data or "").strip(),
                copy_identifier=(form.copy_identifier.data or "").strip(),
                issuing_branch_id=branch_id,
                actor_user_id=user.id if user else None,
            )
            flash(
                f"Issued copy {loan.copy.accession_number} ('{loan.copy.book.title}') "
                f"to {loan.member.member_code} ({loan.member.full_name}). "
                f"Loan #{loan.loan_number} is due on {loan.due_date.isoformat()}.",
                "success",
            )
        except DomainError as exc:
            flash(f"Issue rejected: {exc.message}", "danger")
    else:
        flash_form_errors(form)

    return redirect(url_for("circulation.index"))


@circulation_bp.route("/return", methods=["POST"])
@roles_required(UserRole.ADMIN, UserRole.LIBRARIAN)
def return_copy():
    form = ReturnCopyForm()
    form.returned_branch_id.choices = branch_choices(
        include_any_label="— Default —", active_only=False
    )
    if form.validate_on_submit():
        user = get_current_user()
        raw_ident = (form.identifier.data or "").strip()
        ret_branch = (
            form.returned_branch_id.data
            if form.returned_branch_id.data and form.returned_branch_id.data > 0
            else None
        )
        try:
            if raw_ident.upper().startswith("LN-"):
                outcome = get_services()["circulation"].return_copy(
                    loan_number=raw_ident,
                    returned_branch_id=ret_branch,
                    returned_damaged=bool(form.returned_damaged.data),
                    return_notes=form.return_notes.data,
                    actor_user_id=user.id if user else None,
                )
            else:
                outcome = get_services()["circulation"].return_copy(
                    copy_identifier=raw_ident,
                    returned_branch_id=ret_branch,
                    returned_damaged=bool(form.returned_damaged.data),
                    return_notes=form.return_notes.data,
                    actor_user_id=user.id if user else None,
                )

            msg = (
                f"Returned copy {outcome.loan.copy.accession_number} "
                f"(Loan {outcome.loan.loan_number}). Copy state: {outcome.loan.copy.status}."
            )
            if outcome.fine is not None and outcome.fine.assessed_amount > ZERO_MONEY:
                msg += (
                    f" Overdue fine {outcome.fine.fine_number} of "
                    f"INR {outcome.fine.assessed_amount:.2f} assessed."
                )
            if outcome.allocated_reservation is not None:
                msg += (
                    f" Allocated to waiting reservation "
                    f"{outcome.allocated_reservation.reservation_number} "
                    f"for member {outcome.allocated_reservation.member.member_code}."
                )
            flash(msg, "success")
        except DomainError as exc:
            flash(f"Return rejected: {exc.message}", "danger")
    else:
        flash_form_errors(form)

    return redirect(url_for("circulation.index"))


@circulation_bp.route("/loans/<int:loan_id>/renew", methods=["POST"])
@roles_required(UserRole.ADMIN, UserRole.LIBRARIAN)
def renew(loan_id: int):
    form = ActionCSRFForm()
    if form.validate_on_submit():
        user = get_current_user()
        try:
            loan = get_services()["circulation"].renew_loan(
                loan_id,
                actor_user_id=user.id if user else None,
            )
            flash(
                f"Renewed loan {loan.loan_number} (renewal #{loan.renewal_count}). "
                f"New due date: {loan.due_date.isoformat()}.",
                "success",
            )
        except DomainError as exc:
            flash(f"Renewal rejected: {exc.message}", "danger")
    else:
        flash_form_errors(form)

    return redirect(safe_referrer_or(url_for("circulation.index")))


@circulation_bp.route("/loans/<int:loan_id>/lost", methods=["POST"])
@roles_required(UserRole.ADMIN, UserRole.LIBRARIAN)
def report_lost(loan_id: int):
    form = ReportLostForm()
    if form.validate_on_submit():
        user = get_current_user()
        try:
            loan, fine = get_services()["circulation"].report_loan_copy_lost(
                loan_id,
                notes=form.notes.data,
                actor_user_id=user.id if user else None,
            )
            msg = f"Recorded copy {loan.copy.accession_number} as LOST and closed loan {loan.loan_number}."
            if fine is not None:
                msg += f" Accrued overdue fine {fine.fine_number} (INR {fine.assessed_amount:.2f}) recorded."
            flash(msg, "warning")
        except DomainError as exc:
            flash(f"Lost-copy report rejected: {exc.message}", "danger")
    else:
        flash_form_errors(form)

    return redirect(safe_referrer_or(url_for("circulation.index")))

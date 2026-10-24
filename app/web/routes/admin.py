"""Administration routes for users, branches, tier policies, holidays, audit logs, and corrections (Nav 10)."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.domain.enums import AuditEventType, UserRole
from app.domain.exceptions import DomainError, NotFoundError
from app.web.auth_utils import get_current_user, roles_required
from app.web.forms import (
    ActionCSRFForm,
    AdminAdjustDueDateForm,
    AdminReinstateCopyForm,
    BranchForm,
    BranchUpdateForm,
    HolidayForm,
    StaffUserCreateForm,
    StaffUserUpdateForm,
    TierPolicyForm,
)
from app.web.routes.helpers import branch_choices, flash_form_errors, get_services

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("")
@roles_required(UserRole.ADMIN)
def index():
    svcs = get_services()
    users = svcs["auth"].list_users()
    branches = svcs["admin"].list_branches()
    tiers = svcs["admin"].list_tiers()
    holidays = svcs["admin"].list_holidays()
    recent_audit = svcs["admin"].search_audit_events(page=1, per_page=12)
    return render_template(
        "admin/index.html",
        users=users,
        branches=branches,
        tiers=tiers,
        holidays=holidays,
        recent_audit=recent_audit,
        active_nav="admin",
    )


@admin_bp.route("/users", methods=["GET", "POST"])
@roles_required(UserRole.ADMIN)
def users():
    auth_svc = get_services()["auth"]
    create_form = StaffUserCreateForm()
    create_form.branch_id.choices = branch_choices(
        include_any_label="— Network-Wide (All Branches) —", active_only=False
    )

    if request.method == "POST" and create_form.validate_on_submit():
        actor = get_current_user()
        branch_id = (
            create_form.branch_id.data
            if create_form.branch_id.data and create_form.branch_id.data > 0
            else None
        )
        try:
            user = auth_svc.create_staff_user(
                username=create_form.username.data or "",
                full_name=create_form.full_name.data or "",
                email=create_form.email.data,
                role=create_form.role.data or "LIBRARIAN",
                branch_id=branch_id,
                password=create_form.password.data or "",
                actor_user_id=actor.id if actor else None,
            )
            flash(
                f"Created staff account '{user.username}' ({user.role}).",
                "success",
            )
            return redirect(url_for("admin.users"))
        except (DomainError, ValueError) as exc:
            flash(str(exc), "danger")
    elif request.method == "POST":
        flash_form_errors(create_form)

    staff_users = auth_svc.list_users()
    update_form = StaffUserUpdateForm()
    update_form.branch_id.choices = branch_choices(
        include_any_label="— Network-Wide —", active_only=False
    )
    return render_template(
        "admin/users.html",
        users=staff_users,
        create_form=create_form,
        update_form=update_form,
        branches=get_services()["admin"].list_branches(),
        active_nav="admin",
    )


@admin_bp.route("/users/<int:user_id>/update", methods=["POST"])
@roles_required(UserRole.ADMIN)
def update_user(user_id: int):
    form = StaffUserUpdateForm()
    form.branch_id.choices = branch_choices(
        include_any_label="— Network-Wide —", active_only=False
    )
    if form.validate_on_submit():
        actor = get_current_user()
        branch_id = (
            form.branch_id.data
            if form.branch_id.data and form.branch_id.data > 0
            else None
        )
        try:
            updated = get_services()["auth"].update_staff_user(
                user_id,
                role=form.role.data,
                is_active=bool(form.is_active.data),
                branch_id=branch_id,
                new_password=form.new_password.data,
                actor_user_id=actor.id if actor else None,
            )
            flash(f"Updated staff account '{updated.username}'.", "success")
        except (DomainError, ValueError) as exc:
            flash(str(exc), "danger")
    else:
        flash_form_errors(form)
    return redirect(url_for("admin.users"))


@admin_bp.route("/branches", methods=["GET", "POST"])
@roles_required(UserRole.ADMIN)
def branches():
    admin_svc = get_services()["admin"]
    form = BranchForm()
    if request.method == "POST" and form.validate_on_submit():
        actor = get_current_user()
        try:
            branch = admin_svc.create_branch(
                code=form.code.data or "",
                name=form.name.data or "",
                address=form.address.data or "",
                pincode=form.pincode.data or "",
                phone=form.phone.data or "",
                email=form.email.data or "",
                actor_user_id=actor.id if actor else None,
            )
            flash(f"Created branch '{branch.name}' ({branch.code}).", "success")
            return redirect(url_for("admin.branches"))
        except (DomainError, ValueError) as exc:
            flash(str(exc), "danger")
    elif request.method == "POST":
        flash_form_errors(form)

    return render_template(
        "admin/branches.html",
        branches=admin_svc.list_branches(),
        form=form,
        csrf_form=ActionCSRFForm(),
        active_nav="admin",
    )


@admin_bp.route("/branches/<int:branch_id>/edit", methods=["POST"])
@roles_required(UserRole.ADMIN)
def update_branch(branch_id: int):
    form = BranchUpdateForm()
    if form.validate_on_submit():
        actor = get_current_user()
        try:
            updated = get_services()["admin"].update_branch(
                branch_id,
                name=form.name.data or "",
                address=form.address.data or "",
                pincode=form.pincode.data or "",
                phone=form.phone.data or "",
                email=form.email.data or "",
                is_active=bool(form.is_active.data),
                actor_user_id=actor.id if actor else None,
            )
            flash(f"Updated branch '{updated.name}'.", "success")
        except (DomainError, NotFoundError, ValueError) as exc:
            flash(str(exc), "danger")
    else:
        flash_form_errors(form)
    return redirect(url_for("admin.branches"))


@admin_bp.route("/tiers", methods=["GET"])
@roles_required(UserRole.ADMIN)
def tiers():
    tier_list = get_services()["admin"].list_tiers()
    forms_by_code = {
        t.code: TierPolicyForm(
            max_active_loans=t.max_active_loans,
            loan_duration_days=t.loan_duration_days,
            max_renewals=t.max_renewals,
            renewal_duration_days=t.renewal_duration_days,
            max_active_reservations=t.max_active_reservations,
            grace_period_days=t.grace_period_days,
            daily_fine_rate=t.daily_fine_rate,
            max_fine_per_loan=t.max_fine_per_loan,
            fine_blocking_threshold=t.fine_blocking_threshold,
            borrowing_enabled=t.borrowing_enabled,
        )
        for t in tier_list
    }
    return render_template(
        "admin/tiers.html",
        tiers=tier_list,
        forms_by_code=forms_by_code,
        active_nav="admin",
    )


@admin_bp.route("/tiers/<string:tier_code>", methods=["POST"])
@roles_required(UserRole.ADMIN)
def update_tier(tier_code: str):
    form = TierPolicyForm()
    if form.validate_on_submit():
        actor = get_current_user()
        try:
            tier = get_services()["admin"].update_tier_policy(
                tier_code,
                max_active_loans=int(form.max_active_loans.data or 0),
                loan_duration_days=int(form.loan_duration_days.data or 14),
                max_renewals=int(form.max_renewals.data or 0),
                renewal_duration_days=int(form.renewal_duration_days.data or 7),
                max_active_reservations=int(form.max_active_reservations.data or 0),
                grace_period_days=int(form.grace_period_days.data or 0),
                daily_fine_rate=form.daily_fine_rate.data or 0,
                max_fine_per_loan=form.max_fine_per_loan.data or 0,
                fine_blocking_threshold=form.fine_blocking_threshold.data or 0,
                borrowing_enabled=bool(form.borrowing_enabled.data),
                actor_user_id=actor.id if actor else None,
            )
            flash(f"Updated {tier.code} membership tier policy.", "success")
        except (DomainError, ValueError) as exc:
            flash(str(exc), "danger")
    else:
        flash_form_errors(form)
    return redirect(url_for("admin.tiers"))


@admin_bp.route("/holidays", methods=["GET", "POST"])
@roles_required(UserRole.ADMIN)
def holidays():
    admin_svc = get_services()["admin"]
    form = HolidayForm()
    form.branch_id.choices = branch_choices(
        include_any_label="— Network-Wide Closure (All Branches) —",
        active_only=False,
    )
    csrf_form = ActionCSRFForm()

    if request.method == "POST" and form.validate_on_submit():
        actor = get_current_user()
        br_id = (
            form.branch_id.data
            if form.branch_id.data and form.branch_id.data > 0
            else None
        )
        try:
            hol = admin_svc.create_holiday(
                holiday_date=form.holiday_date.data,
                name=form.name.data or "",
                branch_id=br_id,
                description=form.description.data,
                actor_user_id=actor.id if actor else None,
            )
            flash(
                f"Added holiday '{hol.name}' on {hol.holiday_date.isoformat()}.",
                "success",
            )
            return redirect(url_for("admin.holidays"))
        except (DomainError, ValueError) as exc:
            flash(str(exc), "danger")
    elif request.method == "POST":
        flash_form_errors(form)

    return render_template(
        "admin/holidays.html",
        holidays=admin_svc.list_holidays(),
        form=form,
        csrf_form=csrf_form,
        active_nav="admin",
    )


@admin_bp.route("/holidays/<int:holiday_id>/delete", methods=["POST"])
@roles_required(UserRole.ADMIN)
def delete_holiday(holiday_id: int):
    form = ActionCSRFForm()
    if form.validate_on_submit():
        actor = get_current_user()
        try:
            get_services()["admin"].delete_holiday(
                holiday_id, actor_user_id=actor.id if actor else None
            )
            flash("Holiday deleted from calendar.", "info")
        except (DomainError, ValueError) as exc:
            flash(str(exc), "danger")
    else:
        flash_form_errors(form)
    return redirect(url_for("admin.holidays"))


@admin_bp.route("/audit")
@roles_required(UserRole.ADMIN)
def audit():
    admin_svc = get_services()["admin"]
    event_type = request.args.get("event_type", "").strip()
    entity_type = request.args.get("entity_type", "").strip()
    branch_id_raw = request.args.get("branch_id", "").strip()
    branch_id = (
        int(branch_id_raw)
        if branch_id_raw.isdigit() and int(branch_id_raw) > 0
        else None
    )
    page = max(1, request.args.get("page", 1, type=int))

    events_page = admin_svc.search_audit_events(
        event_type=event_type or None,
        entity_type=entity_type or None,
        branch_id=branch_id,
        page=page,
        per_page=30,
    )
    return render_template(
        "admin/audit.html",
        events_page=events_page,
        selected_event_type=event_type,
        selected_entity_type=entity_type,
        selected_branch_id=branch_id,
        event_types=[e.value for e in AuditEventType],
        branches=admin_svc.list_branches(),
        active_nav="admin",
    )


@admin_bp.route("/corrections", methods=["GET", "POST"])
@roles_required(UserRole.ADMIN)
def corrections():
    reinstate_form = AdminReinstateCopyForm()
    due_form = AdminAdjustDueDateForm()
    action = request.form.get("correction_type")

    if request.method == "POST":
        actor = get_current_user()
        assert actor is not None
        if action == "reinstate_copy":
            if reinstate_form.validate_on_submit():
                try:
                    copy = get_services()["admin"].admin_reinstate_withdrawn_copy(
                        (reinstate_form.copy_id.data or "").strip(),
                        reason=reinstate_form.reason.data or "",
                        actor_user_id=actor.id,
                    )
                    flash(
                        f"Reinstated copy {copy.accession_number} to AVAILABLE and logged ADMIN_CORRECTION.",
                        "success",
                    )
                    return redirect(url_for("admin.corrections"))
                except (DomainError, ValueError) as exc:
                    flash(str(exc), "danger")
            else:
                flash_form_errors(reinstate_form)
        elif action == "adjust_due_date":
            if due_form.validate_on_submit():
                try:
                    loan = get_services()["admin"].admin_adjust_loan_due_date(
                        (due_form.loan_id.data or "").strip(),
                        new_due_date=due_form.new_due_date.data,
                        reason=due_form.reason.data or "",
                        actor_user_id=actor.id,
                    )
                    flash(
                        f"Adjusted due date of loan {loan.loan_number} to {loan.due_date.isoformat()}.",
                        "success",
                    )
                    return redirect(url_for("admin.corrections"))
                except (DomainError, ValueError) as exc:
                    flash(str(exc), "danger")
            else:
                flash_form_errors(due_form)

    return render_template(
        "admin/corrections.html",
        reinstate_form=reinstate_form,
        due_form=due_form,
        active_nav="admin",
    )

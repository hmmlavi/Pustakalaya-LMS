"""Member management routes (Module C)."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.domain.enums import MembershipStatus, TierCode, UserRole
from app.domain.exceptions import DomainError, NotFoundError
from app.domain.value_objects import local_today
from app.web.auth_utils import get_current_user, login_required, roles_required
from app.web.forms import MemberForm, MemberStatusForm
from app.web.routes.helpers import branch_choices, flash_form_errors, get_services

members_bp = Blueprint("members", __name__, url_prefix="/members")


@members_bp.route("")
@login_required
def list_members():
    member_svc = get_services()["members"]
    admin_svc = get_services()["admin"]

    query = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    tier_code = request.args.get("tier", "").strip()
    branch_id_raw = request.args.get("branch_id", "").strip()
    branch_id = (
        int(branch_id_raw)
        if branch_id_raw.isdigit() and int(branch_id_raw) > 0
        else None
    )
    page = max(1, request.args.get("page", 1, type=int))

    results = member_svc.search_members(
        query=query or None,
        status=status or None,
        tier_code=tier_code or None,
        branch_id=branch_id,
        page=page,
        per_page=25,
    )
    return render_template(
        "members/list.html",
        results=results,
        query=query,
        selected_status=status,
        selected_tier=tier_code,
        selected_branch_id=branch_id,
        statuses=[s.value for s in MembershipStatus],
        tiers=[t.value for t in TierCode],
        tier_policies=member_svc.list_tiers(),
        branches=admin_svc.list_branches(),
        active_nav="members",
    )


@members_bp.route("/tiers")
@login_required
def tiers():
    tier_policies = get_services()["members"].list_tiers()
    return render_template(
        "members/tiers.html",
        tiers=tier_policies,
        active_nav="members",
    )


@members_bp.route("/new", methods=["GET", "POST"])
@roles_required(UserRole.ADMIN, UserRole.LIBRARIAN)
def register():
    form = MemberForm()
    form.home_branch_id.choices = branch_choices(
        include_any_label="— Select Home Branch (Optional) —"
    )

    if form.validate_on_submit():
        user = get_current_user()
        home_br = (
            form.home_branch_id.data
            if form.home_branch_id.data and form.home_branch_id.data > 0
            else None
        )
        try:
            member = get_services()["members"].register_member(
                full_name=form.full_name.data or "",
                tier_code=form.tier_code.data or "STANDARD",
                email=form.email.data,
                phone=form.phone.data,
                address=form.address.data,
                home_branch_id=home_br,
                expiry_date=form.expiry_date.data,
                notes=form.notes.data,
                actor_user_id=user.id if user else None,
            )
            flash(
                f"Registered member {member.member_code} ({member.full_name}).",
                "success",
            )
            return redirect(url_for("members.detail", member_id=member.id))
        except (DomainError, ValueError) as exc:
            flash(str(exc), "danger")
    elif request.method == "POST":
        flash_form_errors(form)

    return render_template(
        "members/form.html",
        form=form,
        is_edit=False,
        member=None,
        active_nav="members",
    )


@members_bp.route("/<int:member_id>")
@login_required
def detail(member_id: int):
    try:
        profile = get_services()["members"].get_member_profile(member_id)
    except NotFoundError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("members.list_members"))

    status_form = MemberStatusForm(new_status=profile.member.status)
    return render_template(
        "members/detail.html",
        profile=profile,
        member=profile.member,
        status_form=status_form,
        today=local_today(),
        active_nav="members",
    )


@members_bp.route("/<int:member_id>/edit", methods=["GET", "POST"])
@roles_required(UserRole.ADMIN, UserRole.LIBRARIAN)
def edit(member_id: int):
    member_svc = get_services()["members"]
    try:
        member = member_svc.get_member(member_id)
    except NotFoundError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("members.list_members"))

    if request.method == "GET":
        form = MemberForm(
            full_name=member.full_name,
            tier_code=member.tier.code,
            email=member.email,
            phone=member.phone,
            home_branch_id=member.home_branch_id or 0,
            expiry_date=member.expiry_date,
            address=member.address,
            notes=member.notes,
        )
    else:
        form = MemberForm()
    form.home_branch_id.choices = branch_choices(
        include_any_label="— No Specific Home Branch —", active_only=False
    )

    if form.validate_on_submit():
        user = get_current_user()
        home_br = (
            form.home_branch_id.data
            if form.home_branch_id.data and form.home_branch_id.data > 0
            else None
        )
        try:
            updated = member_svc.update_member(
                member_id,
                full_name=form.full_name.data or "",
                tier_code=form.tier_code.data or "STANDARD",
                email=form.email.data,
                phone=form.phone.data,
                home_branch_id=home_br,
                expiry_date=form.expiry_date.data,
                address=form.address.data,
                notes=form.notes.data,
                actor_user_id=user.id if user else None,
            )
            flash(f"Updated member {updated.member_code}.", "success")
            return redirect(url_for("members.detail", member_id=updated.id))
        except (DomainError, ValueError) as exc:
            flash(str(exc), "danger")
    elif request.method == "POST":
        flash_form_errors(form)

    return render_template(
        "members/form.html",
        form=form,
        is_edit=True,
        member=member,
        active_nav="members",
    )


@members_bp.route("/<int:member_id>/status", methods=["POST"])
@roles_required(UserRole.ADMIN, UserRole.LIBRARIAN)
def change_status(member_id: int):
    form = MemberStatusForm()
    if form.validate_on_submit():
        user = get_current_user()
        try:
            member = get_services()["members"].change_membership_status(
                member_id,
                form.new_status.data or "",
                reason=form.reason.data,
                actor_user_id=user.id if user else None,
            )
            flash(
                f"Membership status for {member.member_code} changed to {member.status}.",
                "success",
            )
        except (DomainError, ValueError) as exc:
            flash(str(exc), "danger")
    else:
        flash_form_errors(form)
    return redirect(url_for("members.detail", member_id=member_id))

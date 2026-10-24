"""Authentication routes (login, first-run admin setup, and CSRF-protected logout)."""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from app.domain.exceptions import AuthenticationError, AuthorizationError, DomainError
from app.web.auth_utils import get_current_user, login_required
from app.web.forms import ActionCSRFForm, FirstRunSetupForm, LoginForm
from app.web.routes.helpers import (
    flash_form_errors,
    get_services,
    is_safe_redirect_target,
)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if get_current_user() is not None:
        return redirect(url_for("dashboard.index"))

    form = LoginForm()
    next_target = request.args.get("next") or request.form.get("next")
    auth_svc = get_services()["auth"]
    has_any_users = auth_svc.count_users() > 0

    if form.validate_on_submit():
        try:
            user = auth_svc.authenticate(
                form.username.data or "",
                form.password.data or "",
            )
            session.clear()
            session.permanent = True
            session["user_id"] = user.id
            flash(f"Welcome back, {user.full_name} ({user.role}).", "success")
            if is_safe_redirect_target(next_target):
                return redirect(next_target)  # type: ignore[arg-type]
            return redirect(url_for("dashboard.index"))
        except AuthenticationError as exc:
            flash(exc.message, "danger")
    elif request.method == "POST":
        flash_form_errors(form)

    return render_template(
        "auth/login.html",
        form=form,
        next_target=next_target if is_safe_redirect_target(next_target) else "",
        has_any_users=has_any_users,
    )


@auth_bp.route("/setup", methods=["GET", "POST"])
def first_run_setup():
    auth_svc = get_services()["auth"]
    if auth_svc.count_users() > 0:
        if request.method == "POST":
            abort(403)
        flash("Initial administrator setup has already been completed.", "info")
        return redirect(url_for("auth.login"))

    form = FirstRunSetupForm()
    if form.validate_on_submit():
        try:
            user = auth_svc.create_first_run_admin(
                username=form.username.data or "",
                full_name=form.full_name.data or "",
                password=form.password.data or "",
                email=form.email.data or None,
            )
            session.clear()
            session.permanent = True
            session["user_id"] = user.id
            flash(
                f"Initial Administrator '{user.username}' created. Welcome to Pustakalaya!",
                "success",
            )
            return redirect(url_for("dashboard.index"))
        except AuthorizationError:
            abort(403)
        except (DomainError, ValueError) as exc:
            flash(str(exc), "danger")
    elif request.method == "POST":
        flash_form_errors(form)

    return render_template("auth/setup.html", form=form)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    form = ActionCSRFForm()
    if form.validate_on_submit():
        session.clear()
        flash("You have been signed out safely.", "info")
    return redirect(url_for("auth.login"))

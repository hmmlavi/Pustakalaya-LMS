"""Settings, appearance preferences, and personal account configuration routes (Navigation section 11)."""

from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from app.domain.exceptions import DomainError
from app.domain.value_objects import local_today
from app.web.auth_utils import get_current_user, login_required
from app.web.forms import AppearancePreferencesForm, ChangePasswordForm
from app.web.routes.helpers import flash_form_errors, get_services, safe_referrer_or

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")

_COOKIE_MAX_AGE = 365 * 24 * 60 * 60


@settings_bp.route("", methods=["GET", "POST"])
@login_required
def index():
    pw_form = ChangePasswordForm()
    user = get_current_user()
    assert user is not None

    if request.method == "POST":
        if pw_form.validate_on_submit():
            try:
                get_services()["auth"].change_own_password(
                    user.id,
                    current_password=pw_form.current_password.data or "",
                    new_password=pw_form.new_password.data or "",
                )
                flash("Your password has been updated.", "success")
                return redirect(url_for("settings.index"))
            except (DomainError, ValueError) as exc:
                flash(str(exc), "danger")
        else:
            flash_form_errors(pw_form)

    raw_theme = (request.cookies.get("pustakalaya_theme") or "system").strip().lower()
    if raw_theme not in ("day", "night", "system"):
        raw_theme = "system"
    raw_amb = (request.cookies.get("pustakalaya_ambience") or "auto").strip().lower()
    if raw_amb not in ("auto", "minimal", "off"):
        raw_amb = "auto"

    appearance_form = AppearancePreferencesForm(
        theme_preference=raw_theme,
        ambience_mode=raw_amb,
    )

    tz_name = current_app.config.get("LIBRARY_TIMEZONE", "Asia/Kolkata")
    tiers = get_services()["members"].list_tiers()
    branches = get_services()["admin"].list_branches()

    return render_template(
        "settings/index.html",
        pw_form=pw_form,
        appearance_form=appearance_form,
        library_timezone=tz_name,
        local_date=local_today(tz_name),
        pickup_hold_days=current_app.config.get("PICKUP_HOLD_DAYS", 3),
        tiers=tiers,
        branches=branches,
        active_nav="settings",
    )


@settings_bp.route("/appearance", methods=["POST"])
@login_required
def update_appearance():
    form = AppearancePreferencesForm()
    target_url = safe_referrer_or(url_for("settings.index"))
    if not form.validate_on_submit():
        flash_form_errors(form)
        return redirect(target_url)

    theme_pref = (form.theme_preference.data or "system").strip().lower()
    ambience_mode = (form.ambience_mode.data or "auto").strip().lower()
    secure_cookie = bool(current_app.config.get("SESSION_COOKIE_SECURE", False))

    resp = make_response(redirect(target_url))
    resp.set_cookie(
        "pustakalaya_theme",
        theme_pref,
        max_age=_COOKIE_MAX_AGE,
        path="/",
        secure=secure_cookie,
        httponly=False,
        samesite="Lax",
    )
    resp.set_cookie(
        "pustakalaya_ambience",
        ambience_mode,
        max_age=_COOKIE_MAX_AGE,
        path="/",
        secure=secure_cookie,
        httponly=False,
        samesite="Lax",
    )
    flash(
        f"Saved appearance preferences (Theme: {theme_pref.title()}, Library Ambience: {ambience_mode.title()}).",
        "success",
    )
    return resp

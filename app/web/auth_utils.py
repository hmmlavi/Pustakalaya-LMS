"""Authentication session helpers and server-side role-based access decorators."""

from __future__ import annotations

from functools import wraps
from typing import Callable

from flask import abort, current_app, flash, g, redirect, request, session, url_for

from app.domain.enums import UserRole
from app.models import User


def get_current_user() -> User | None:
    return getattr(g, "current_user", None)


def load_logged_in_user() -> None:
    """Reload and verify the active session user before each request."""
    user_id = session.get("user_id")
    if user_id is None:
        g.current_user = None
        return

    auth_service = current_app.extensions["services"]["auth"]
    user = auth_service.get_active_user(int(user_id))
    if user is None:
        session.clear()
        g.current_user = None
        return
    g.current_user = user


def login_required(view: Callable):
    """Require an authenticated active staff session."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if get_current_user() is None:
            flash("Please sign in to access Pustakalaya.", "warning")
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


def roles_required(*allowed_roles: UserRole):
    """Enforce server-side role authorization, returning HTTP 403 when unauthorized."""

    def decorator(view: Callable):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = get_current_user()
            if user is None:
                flash("Please sign in to continue.", "warning")
                return redirect(url_for("auth.login", next=request.full_path))
            if not user.has_role(*allowed_roles):
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator

"""Shared route helpers for safe redirects, branch dropdowns, and form errors."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from flask import current_app, flash, request
from flask_wtf import FlaskForm
from wtforms.fields.choices import SelectChoice


def get_services() -> dict[str, Any]:
    return current_app.extensions["services"]


def is_safe_redirect_target(target: str | None) -> bool:
    """Prevent open-redirect vulnerabilities on login `next` and referrer parameters."""
    if not target:
        return False
    if "\\" in target or target.startswith("//"):
        return False
    if any(ord(ch) < 32 for ch in target):
        return False
    parts = urlsplit(target)
    return not parts.scheme and not parts.netloc and target.startswith("/")


def safe_referrer_or(default_url: str) -> str:
    """Return a validated same-host relative path from request.referrer, else default_url."""
    ref = request.referrer
    if not ref:
        return default_url
    ref_parts = urlsplit(ref)
    host_parts = urlsplit(request.host_url)
    if (
        ref_parts.scheme == host_parts.scheme
        and ref_parts.netloc == host_parts.netloc
        and ref_parts.path
    ):
        rel = (
            f"{ref_parts.path}?{ref_parts.query}"
            if ref_parts.query
            else ref_parts.path
        )
        if is_safe_redirect_target(rel):
            return rel
    return default_url


def branch_choices(
    *, include_any_label: str | None = None, active_only: bool = True
) -> list[SelectChoice]:
    branches = get_services()["admin"].list_branches(active_only=active_only)
    choices: list[SelectChoice] = []
    if include_any_label is not None:
        choices.append(SelectChoice(0, include_any_label))
    for br in branches:
        choices.append(SelectChoice(br.id, f"{br.code} — {br.name}"))
    return choices


def flash_form_errors(form: FlaskForm) -> None:
    for field_name, errors in form.errors.items():
        label = getattr(form, field_name).label.text if hasattr(form, field_name) else field_name
        for err in errors:
            flash(f"{label}: {err}", "danger")

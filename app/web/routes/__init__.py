"""Blueprint registration for Pustakalaya web interface."""

from __future__ import annotations

from flask import Flask

from app.web.routes.admin import admin_bp
from app.web.routes.auth import auth_bp
from app.web.routes.books import books_bp
from app.web.routes.circulation import circulation_bp
from app.web.routes.copies import copies_bp
from app.web.routes.dashboard import dashboard_bp
from app.web.routes.data_quality import data_quality_bp
from app.web.routes.fines import fines_bp
from app.web.routes.members import members_bp
from app.web.routes.reports import reports_bp
from app.web.routes.reservations import reservations_bp
from app.web.routes.settings import settings_bp


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(books_bp)
    app.register_blueprint(copies_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(circulation_bp)
    app.register_blueprint(reservations_bp)
    app.register_blueprint(fines_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(data_quality_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(settings_bp)

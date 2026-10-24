"""Flask application factory for Pustakalaya."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, render_template, request
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import AppConfig
from app.domain.enums import UserRole
from app.domain.value_objects import ZERO_MONEY, quantize_money
from app.infrastructure.database import create_db_engine, create_session_factory
from app.infrastructure.logging_config import configure_logging
from app.services import (
    AdministrationService,
    AuthenticationService,
    BookService,
    CirculationService,
    DataImportService,
    FineService,
    MemberService,
    ReportingService,
    ReservationService,
)
from app.web.auth_utils import get_current_user, load_logged_in_user
from app.web.forms import ActionCSRFForm
from app.web.routes import register_blueprints

csrf = CSRFProtect()
logger = logging.getLogger("pustakalaya.app")


def create_app(config: AppConfig | None = None) -> Flask:
    """Create and configure a Pustakalaya Flask application instance."""
    app_cfg = config or AppConfig()

    log_path = None if app_cfg.TESTING else (app_cfg.DATA_DIR / "pustakalaya.log")
    configure_logging(log_file=log_path)

    if app_cfg.EPHEMERAL_SECRET_WARNING and not app_cfg.TESTING:
        logger.warning(
            "SECRET_KEY was not configured in .env; generated an ephemeral session key for this process."
        )

    web_dir = Path(__file__).resolve().parent / "web"
    app = Flask(
        __name__,
        template_folder=str(web_dir / "templates"),
        static_folder=str(web_dir / "static"),
    )
    app.config.from_mapping(app_cfg.to_flask_mapping())
    if not app_cfg.TESTING:
        inner_wsgi = app.wsgi_app

        def _preview_iframe_cookie_wsgi(environ, start_response):
            is_https_dev = (
                app_cfg.ENV == "development"
                and environ.get("wsgi.url_scheme") == "https"
            )

            def _wrapped_start_response(status, headers, exc_info=None):
                if is_https_dev:
                    new_headers = []
                    for k, v in headers:
                        if k.lower() == "set-cookie":
                            updated = v.replace("SameSite=Lax", "SameSite=None")
                            if "SameSite=None" not in updated:
                                updated += "; SameSite=None"
                            if "; Secure" not in updated:
                                updated += "; Secure"
                            if "; Partitioned" not in updated:
                                updated += "; Partitioned"
                            new_headers.append((k, updated))
                        else:
                            new_headers.append((k, v))
                    headers = new_headers
                return start_response(status, headers, exc_info)

            return inner_wsgi(environ, _wrapped_start_response)

        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            _preview_iframe_cookie_wsgi, x_for=1, x_proto=1, x_host=1
        )

    csrf.init_app(app)

    engine = create_db_engine(app_cfg.DATABASE_URL)
    session_factory = create_session_factory(engine)

    fine_service = FineService(
        session_factory, library_timezone=app_cfg.LIBRARY_TIMEZONE
    )
    reservation_service = ReservationService(
        session_factory,
        library_timezone=app_cfg.LIBRARY_TIMEZONE,
        pickup_hold_days=app_cfg.PICKUP_HOLD_DAYS,
    )
    circulation_service = CirculationService(
        session_factory,
        fine_service=fine_service,
        reservation_service=reservation_service,
        library_timezone=app_cfg.LIBRARY_TIMEZONE,
        pickup_hold_days=app_cfg.PICKUP_HOLD_DAYS,
    )

    app.extensions["db_engine"] = engine
    app.extensions["session_factory"] = session_factory
    app.extensions["services"] = {
        "auth": AuthenticationService(session_factory),
        "admin": AdministrationService(
            session_factory, library_timezone=app_cfg.LIBRARY_TIMEZONE
        ),
        "books": BookService(
            session_factory,
            reservation_service=reservation_service,
            library_timezone=app_cfg.LIBRARY_TIMEZONE,
        ),
        "members": MemberService(
            session_factory, library_timezone=app_cfg.LIBRARY_TIMEZONE
        ),
        "circulation": circulation_service,
        "reservations": reservation_service,
        "fines": fine_service,
        "reporting": ReportingService(
            session_factory,
            fine_service=fine_service,
            library_timezone=app_cfg.LIBRARY_TIMEZONE,
        ),
        "imports": DataImportService(
            session_factory, library_timezone=app_cfg.LIBRARY_TIMEZONE
        ),
    }

    app.before_request(load_logged_in_user)
    register_blueprints(app)

    @app.after_request
    def apply_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        if (
            get_current_user() is not None
            and response.mimetype == "text/html"
            and "Cache-Control" not in response.headers
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.template_filter("inr")
    def format_inr(value: Decimal | int | str | None) -> str:
        if value is None:
            amt = ZERO_MONEY
        else:
            amt = quantize_money(value)
        return f"₹{amt:,.2f}"

    @app.template_filter("ist_dt")
    def format_ist_datetime(value: datetime | None) -> str:
        if value is None:
            return "—"
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        local_dt = value.astimezone(ZoneInfo(app_cfg.LIBRARY_TIMEZONE))
        return local_dt.strftime("%d %b %Y, %H:%M IST")

    @app.context_processor
    def inject_globals() -> dict[str, object]:
        raw_theme = (request.cookies.get("pustakalaya_theme") or "").strip().lower()
        theme_pref = raw_theme if raw_theme in ("day", "night", "system") else "system"
        resolved_theme = "night" if theme_pref == "night" else "day"
        raw_ambience = (
            (request.cookies.get("pustakalaya_ambience") or "").strip().lower()
        )
        ambience_mode = (
            raw_ambience if raw_ambience in ("auto", "minimal", "off") else "auto"
        )
        return {
            "current_user": get_current_user(),
            "UserRole": UserRole,
            "logout_form": ActionCSRFForm(),
            "app_name": app_cfg.APP_NAME,
            "app_tagline": app_cfg.APP_TAGLINE,
            "network_name": app_cfg.NETWORK_NAME,
            "initial_theme_pref": theme_pref,
            "initial_resolved_theme": resolved_theme,
            "initial_ambience": ambience_mode,
        }

    @app.errorhandler(CSRFError)
    def handle_csrf_error(err: CSRFError):
        logger.warning("CSRF validation failed: %s", err.description)
        return (
            render_template(
                "errors/error.html",
                status_code=400,
                title="Security Token Invalid or Expired",
                message=(
                    "Your form security token (CSRF) was missing or expired. "
                    "Please go back, refresh the page, and submit the form again."
                ),
            ),
            400,
        )

    @app.errorhandler(403)
    def handle_forbidden(_err):
        return (
            render_template(
                "errors/error.html",
                status_code=403,
                title="Access Restricted",
                message=(
                    "Your staff role does not have permission to perform this operation. "
                    "Contact a Library Administrator if elevated access is required."
                ),
            ),
            403,
        )

    @app.errorhandler(404)
    def handle_not_found(_err):
        return (
            render_template(
                "errors/error.html",
                status_code=404,
                title="Page or Record Not Found",
                message="The requested library page or resource could not be found.",
            ),
            404,
        )

    @app.errorhandler(500)
    def handle_internal_error(err):
        logger.exception("Unhandled server error: %s", err)
        return (
            render_template(
                "errors/error.html",
                status_code=500,
                title="Unexpected Application Error",
                message=(
                    "An unexpected internal error occurred and any incomplete database "
                    "changes were rolled back safely. Details have been recorded in the server log."
                ),
            ),
            500,
        )

    return app

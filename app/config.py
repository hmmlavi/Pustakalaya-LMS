"""Application configuration management for Pustakalaya."""

from __future__ import annotations

import os
import secrets
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = BASE_DIR / "instance"

# Load .env from project root if present without overriding explicit environment variables
load_dotenv(BASE_DIR / ".env", override=False)

_PLACEHOLDER_SECRETS = {
    "",
    "replace-with-a-random-64-hex-character-secret-key",
    "changeme",
    "secret",
    "dev",
}


def resolve_database_url(raw_url: str | None = None, *, data_dir: Path | None = None) -> str:
    """Resolve SQLite relative URLs cleanly across Windows and POSIX platforms."""
    runtime_dir = data_dir or INSTANCE_DIR
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if not raw_url:
        db_file = (runtime_dir / "pustakalaya.db").resolve()
        return f"sqlite:///{db_file.as_posix()}"

    if raw_url.startswith("sqlite:///") and not raw_url.startswith("sqlite:///:memory:"):
        rel_part = raw_url[len("sqlite:///") :]
        candidate = Path(rel_part)
        if not candidate.is_absolute():
            # The template uses sqlite:///instance/... for source checkouts. In
            # an installed build, INSTANCE_DIR is replaced by DATA_DIR, so do
            # not create an accidental DATA_DIR/instance/... nesting.
            if candidate.parts and candidate.parts[0].lower() == "instance":
                candidate = Path(*candidate.parts[1:])
            resolved = (runtime_dir / candidate).resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite:///{resolved.as_posix()}"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{candidate.as_posix()}"
    return raw_url


class AppConfig:
    """Default operational configuration."""

    APP_NAME: str = "Pustakalaya"
    APP_TAGLINE: str = "Every Book, One Place."
    NETWORK_NAME: str = "Lucknow Public Library Network"

    def __init__(
        self,
        *,
        database_url: str | None = None,
        secret_key: str | None = None,
        testing: bool = False,
        csrf_enabled: bool = True,
        library_timezone: str | None = None,
        pickup_hold_days: int | None = None,
        historical_source_path: Path | None = None,
        data_dir: Path | None = None,
    ) -> None:
        env_name = os.getenv("FLASK_ENV", "development").strip().lower()
        self.ENV = "testing" if testing else env_name
        self.TESTING = testing
        self.DEBUG = self.ENV == "development" and not testing

        configured_secret = (
            secret_key if secret_key is not None else os.getenv("SECRET_KEY", "").strip()
        )
        if configured_secret in _PLACEHOLDER_SECRETS:
            if self.ENV == "production":
                raise RuntimeError(
                    "SECRET_KEY must be explicitly set to a strong random secret in production."
                )
            self.SECRET_KEY = secrets.token_hex(32)
            self.EPHEMERAL_SECRET_WARNING = True
        else:
            self.SECRET_KEY = configured_secret
            self.EPHEMERAL_SECRET_WARNING = False

        configured_data_dir = data_dir or (
            Path(os.getenv("PUSTAKALAYA_DATA_DIR")).expanduser()
            if os.getenv("PUSTAKALAYA_DATA_DIR")
            else INSTANCE_DIR
        )
        self.DATA_DIR = configured_data_dir.resolve()
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)

        raw_db_url = database_url if database_url is not None else os.getenv("DATABASE_URL")
        self.DATABASE_URL = resolve_database_url(raw_db_url, data_dir=self.DATA_DIR)

        tz_name = library_timezone or os.getenv("LIBRARY_TIMEZONE", "Asia/Kolkata")
        # Validate timezone immediately
        ZoneInfo(tz_name)
        self.LIBRARY_TIMEZONE = tz_name

        lifetime_minutes = int(os.getenv("SESSION_LIFETIME_MINUTES", "480"))
        self.PERMANENT_SESSION_LIFETIME = timedelta(minutes=max(15, lifetime_minutes))
        self.SESSION_COOKIE_HTTPONLY = True
        self.SESSION_COOKIE_SAMESITE = "Lax"
        self.SESSION_COOKIE_SECURE = self.ENV == "production" or os.getenv(
            "SESSION_COOKIE_SECURE", ""
        ).strip().lower() in ("1", "true", "yes")
        self.SESSION_COOKIE_NAME = "pustakalaya_session"

        self.WTF_CSRF_ENABLED = csrf_enabled
        self.WTF_CSRF_TIME_LIMIT = 7200
        self.WTF_CSRF_SSL_STRICT = False

        self.PICKUP_HOLD_DAYS = (
            pickup_hold_days
            if pickup_hold_days is not None
            else int(os.getenv("PICKUP_HOLD_DAYS", "3"))
        )

        self.SEED_RANDOM_SEED = int(os.getenv("SEED_RANDOM_SEED", "20261024"))
        ref_date_raw = os.getenv("HISTORICAL_REFERENCE_DATE", "2026-10-24")
        self.HISTORICAL_REFERENCE_DATE = date.fromisoformat(ref_date_raw)
        self.HISTORICAL_DAYS = int(os.getenv("HISTORICAL_DAYS", "365"))
        self.HISTORICAL_SOURCE_PATH = (
            historical_source_path
            if historical_source_path is not None
            else (self.DATA_DIR / "historical_source_records.json")
        )

    def to_flask_mapping(self) -> dict[str, object]:
        return {
            key: getattr(self, key)
            for key in dir(self)
            if key.isupper() and not key.startswith("_")
        }

"""Initialize the Pustakalaya SQLite database via Alembic migrations and optional admin setup."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import AppConfig
from app.infrastructure.database import (
    apply_migrations,
    create_db_engine,
    create_session_factory,
)
from app.services.authentication_service import AuthenticationService


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply Alembic migrations and optionally bootstrap the initial Administrator."
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Optional SQLAlchemy database URL override (default: from .env or sqlite:///instance/pustakalaya.db).",
    )
    parser.add_argument(
        "--admin-username",
        default=os.getenv("PUSTAKALAYA_ADMIN_USERNAME"),
        help="Initial administrator username.",
    )
    parser.add_argument(
        "--admin-full-name",
        default=os.getenv("PUSTAKALAYA_ADMIN_FULL_NAME", "Chief Library Administrator"),
        help="Full name for the initial administrator.",
    )
    parser.add_argument(
        "--admin-email",
        default=os.getenv("PUSTAKALAYA_ADMIN_EMAIL"),
        help="Optional email for the initial administrator.",
    )
    parser.add_argument(
        "--admin-password",
        default=os.getenv("PUSTAKALAYA_ADMIN_PASSWORD"),
        help="Initial administrator password (prefer env var or --prompt-admin-password).",
    )
    parser.add_argument(
        "--prompt-admin-password",
        action="store_true",
        help="Interactively prompt for the initial administrator password.",
    )
    args = parser.parse_args()

    cfg = AppConfig(database_url=args.database_url)
    resolved_url = apply_migrations(cfg.DATABASE_URL)
    print(f"[OK] Alembic migrations applied up to 'head' on {resolved_url}")

    admin_user = args.admin_username
    admin_pass = args.admin_password
    if args.prompt_admin_password:
        if not admin_user:
            admin_user = input("Administrator username [admin]: ").strip() or "admin"
        pw1 = getpass.getpass("Enter administrator password (min 8 chars): ")
        pw2 = getpass.getpass("Confirm administrator password: ")
        if pw1 != pw2:
            print("[ERROR] Passwords did not match.", file=sys.stderr)
            return 1
        admin_pass = pw1

    if admin_user:
        if not admin_pass:
            print(
                "[ERROR] Provide --prompt-admin-password or PUSTAKALAYA_ADMIN_PASSWORD to create the admin user.",
                file=sys.stderr,
            )
            return 1
        engine = create_db_engine(resolved_url)
        try:
            session_factory = create_session_factory(engine)
            auth_svc = AuthenticationService(session_factory)
            user, created = auth_svc.bootstrap_initial_admin(
                username=admin_user,
                full_name=args.admin_full_name,
                password=admin_pass,
                email=args.admin_email,
            )
            if created:
                print(
                    f"[OK] Created initial Administrator '{user.username}' (role={user.role})."
                )
            else:
                print(
                    f"[OK] Administrator '{user.username}' already exists; left unchanged."
                )
        finally:
            engine.dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared pytest fixtures using isolated temporary SQLite databases."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app import create_app
from app.config import AppConfig
from app.domain.enums import UserRole
from app.infrastructure.database import create_db_engine, create_session_factory
from app.infrastructure.transactions import write_transaction
from app.models import Base, MembershipTier
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


@pytest.fixture()
def db_env(tmp_path: Path):
    """Provision an isolated file-backed SQLite database with core reference data."""
    db_file = tmp_path / "test_pustakalaya.db"
    db_url = f"sqlite:///{db_file.as_posix()}"
    engine = create_db_engine(db_url)
    Base.metadata.create_all(engine)
    sf = create_session_factory(engine)

    auth_svc = AuthenticationService(sf)
    admin_svc = AdministrationService(sf)
    res_svc = ReservationService(sf, pickup_hold_days=3)
    book_svc = BookService(sf, reservation_service=res_svc)
    member_svc = MemberService(sf)
    fine_svc = FineService(sf)
    circ_svc = CirculationService(
        sf, fine_service=fine_svc, reservation_service=res_svc, pickup_hold_days=3
    )
    report_svc = ReportingService(sf, fine_service=fine_svc)
    import_svc = DataImportService(sf)

    admin_user, _ = auth_svc.bootstrap_initial_admin(
        username="admin",
        password="AdminTestPassword!99",
        full_name="Test Administrator",
        email="admin@pustakalaya.test",
    )

    b1 = admin_svc.create_branch(
        code="LKO-HZR",
        name="Hazratganj Central Library",
        address="MG Marg, Hazratganj",
        pincode="226001",
        phone="+91-522-2620101",
        email="hzr@pustakalaya.test",
        actor_user_id=admin_user.id,
    )
    b2 = admin_svc.create_branch(
        code="LKO-GMN",
        name="Gomti Nagar Civic Branch",
        address="Vipul Khand, Gomti Nagar",
        pincode="226010",
        phone="+91-522-2304410",
        email="gmn@pustakalaya.test",
        actor_user_id=admin_user.id,
    )
    b3 = admin_svc.create_branch(
        code="LKO-ALG",
        name="Aliganj Neighbourhood Branch",
        address="Sector E, Aliganj",
        pincode="226024",
        phone="+91-522-2745820",
        email="alg@pustakalaya.test",
        actor_user_id=admin_user.id,
    )

    with write_transaction(sf) as session:
        session.add_all(
            [
                MembershipTier(
                    code="BASIC",
                    name="Basic Citizen",
                    description="Basic tier",
                    max_active_loans=2,
                    loan_duration_days=14,
                    max_renewals=1,
                    renewal_duration_days=7,
                    max_active_reservations=2,
                    grace_period_days=1,
                    daily_fine_rate=Decimal("5.00"),
                    max_fine_per_loan=Decimal("150.00"),
                    fine_blocking_threshold=Decimal("50.00"),
                    borrowing_enabled=True,
                ),
                MembershipTier(
                    code="STANDARD",
                    name="Standard Reader",
                    description="Standard tier",
                    max_active_loans=5,
                    loan_duration_days=21,
                    max_renewals=2,
                    renewal_duration_days=14,
                    max_active_reservations=4,
                    grace_period_days=2,
                    daily_fine_rate=Decimal("3.00"),
                    max_fine_per_loan=Decimal("200.00"),
                    fine_blocking_threshold=Decimal("100.00"),
                    borrowing_enabled=True,
                ),
                MembershipTier(
                    code="PREMIUM",
                    name="Premium Scholar",
                    description="Premium tier",
                    max_active_loans=10,
                    loan_duration_days=30,
                    max_renewals=3,
                    renewal_duration_days=15,
                    max_active_reservations=8,
                    grace_period_days=3,
                    daily_fine_rate=Decimal("2.00"),
                    max_fine_per_loan=Decimal("250.00"),
                    fine_blocking_threshold=Decimal("200.00"),
                    borrowing_enabled=True,
                ),
            ]
        )

    librarian_user = auth_svc.create_staff_user(
        username="librarian",
        password="LibrarianPassword!99",
        full_name="Test Librarian",
        role=UserRole.LIBRARIAN,
        email="librarian@pustakalaya.test",
        branch_id=b1.id,
        actor_user_id=admin_user.id,
    )
    readonly_user = auth_svc.create_staff_user(
        username="readonly",
        password="ReadonlyPassword!99",
        full_name="Test Readonly Staff",
        role=UserRole.STAFF_READONLY,
        email="readonly@pustakalaya.test",
        branch_id=b1.id,
        actor_user_id=admin_user.id,
    )

    yield {
        "db_file": db_file,
        "db_url": db_url,
        "engine": engine,
        "sf": sf,
        "auth": auth_svc,
        "admin": admin_svc,
        "books": book_svc,
        "members": member_svc,
        "circulation": circ_svc,
        "reservations": res_svc,
        "fines": fine_svc,
        "reports": report_svc,
        "imports": import_svc,
        "admin_user": admin_user,
        "librarian_user": librarian_user,
        "readonly_user": readonly_user,
        "branches": (b1, b2, b3),
    }

    engine.dispose()


@pytest.fixture()
def web_app(db_env, tmp_path: Path):
    """Create a Flask test app wired to the isolated SQLite DB with CSRF enabled."""
    cfg = AppConfig(
        secret_key="test-secret-key-for-pustakalaya-suite",
        database_url=db_env["db_url"],
        testing=True,
        csrf_enabled=True,
        historical_source_path=tmp_path / "historical_fixture.json",
    )
    app = create_app(cfg)
    yield app
    app.extensions["db_engine"].dispose()

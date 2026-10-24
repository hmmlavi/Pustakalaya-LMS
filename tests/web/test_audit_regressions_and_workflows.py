"""Audit regression tests, DOM accessibility audit, security hardening checks, and 12 web workflows."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser
import json
from pathlib import Path
import re

from alembic import command
from alembic.config import Config as AlembicConfig
import pytest
from sqlalchemy import func, select

from app import create_app
from app.config import BASE_DIR, AppConfig
from app.domain.enums import (
    CopyStatus,
    FineStatus,
    LoanStatus,
    PaymentMethod,
    ReservationStatus,
)
from app.domain.exceptions import (
    DuplicateRecordError,
    InvalidStateTransitionError,
)
from app.infrastructure.seed import seed_operational_dataset
from app.infrastructure.transactions import read_session, write_transaction
from app.models import (
    Book,
    BookCopy,
    Fine,
    FinePayment,
    LibraryBranch,
    Loan,
    Member,
    MembershipTier,
    Reservation,
)
from app.services import DataImportService
from app.web.routes.helpers import is_safe_redirect_target
from scripts.verify_data import run_verification


class _DOMAccessibilityParser(HTMLParser):
    """Collect element IDs, label[for] targets, and skip-link hrefs from rendered HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.label_fors: list[str] = []
        self.skip_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        elem_id = attr_map.get("id")
        if elem_id:
            self.ids.append(elem_id)
        if tag == "label" and attr_map.get("for"):
            self.label_fors.append(attr_map["for"] or "")
        if (
            tag == "a"
            and "skip-link" in (attr_map.get("class") or "")
            and attr_map.get("href")
        ):
            self.skip_links.append(attr_map["href"] or "")


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    if not match:
        match = re.search(r'value="([^"]+)"[^>]*name="csrf_token"', html)
    assert match is not None, "Expected hidden csrf_token input in HTML"
    return match.group(1)


def _login(client, username: str, password: str):
    page = client.get("/auth/login")
    assert page.status_code == 200
    token = _extract_csrf(page.get_data(as_text=True))
    return client.post(
        "/auth/login",
        data={"username": username, "password": password, "csrf_token": token},
        follow_redirects=True,
    )


def test_regression_new_or_repaired_available_copy_allocates_waiting_fifo_hold(db_env):
    """Adding an AVAILABLE copy or repairing a DAMAGED copy must fulfill the earliest WAITING hold."""
    books = db_env["books"]
    members = db_env["members"]
    circ = db_env["circulation"]
    res_svc = db_env["reservations"]
    b1 = db_env["branches"][0]
    admin_id = db_env["admin_user"].id

    book = books.create_book(
        title="Awadh Architectural Heritage",
        isbn="978-0-306-40615-7",
        publisher="Lucknow Heritage Press",
        publication_year=2021,
        language="English",
        category="History",
        author_names=["Roshan Taqui"],
        actor_user_id=admin_id,
    )
    copy_on_loan = books.add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-FIFO01",
        condition_notes="Good hardcover",
        actor_user_id=admin_id,
    )

    m_borrower = members.register_member(
        full_name="Arjun Verma",
        email="arjun.fifo@test.local",
        phone="9876501001",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        actor_user_id=admin_id,
    )
    m_waiter_1 = members.register_member(
        full_name="Neha Srivastava",
        email="neha.fifo@test.local",
        phone="9876501002",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        actor_user_id=admin_id,
    )
    m_waiter_2 = members.register_member(
        full_name="Kabir Kidwai",
        email="kabir.fifo@test.local",
        phone="9876501003",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        actor_user_id=admin_id,
    )

    circ.issue_copy(
        member_identifier=m_borrower.member_code,
        copy_identifier=copy_on_loan.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=admin_id,
    )
    r1 = res_svc.create_reservation(
        member_identifier=m_waiter_1.member_code,
        book_id=book.id,
        preferred_branch_id=b1.id,
        actor_user_id=admin_id,
    )
    r2 = res_svc.create_reservation(
        member_identifier=m_waiter_2.member_code,
        book_id=book.id,
        preferred_branch_id=b1.id,
        actor_user_id=admin_id,
    )
    assert r1.status == ReservationStatus.WAITING.value
    assert r2.status == ReservationStatus.WAITING.value

    # Case A: Acquiring a brand-new AVAILABLE copy immediately allocates r1 (earliest FIFO waiter)
    added_copy = books.add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-FIFO02",
        condition_notes="Brand new second copy",
        status=CopyStatus.AVAILABLE,
        actor_user_id=admin_id,
    )
    assert added_copy.status == CopyStatus.RESERVED.value
    r1_after = res_svc.get_reservation(r1.id)
    r2_after = res_svc.get_reservation(r2.id)
    assert r1_after.status == ReservationStatus.READY_FOR_PICKUP.value
    assert r1_after.allocated_copy_id == added_copy.id
    assert r1_after.pickup_deadline_at is not None
    assert r2_after.status == ReservationStatus.WAITING.value

    # Case B: Returning copy_on_loan as DAMAGED keeps r2 WAITING; repairing it to AVAILABLE allocates r2!
    circ.return_copy(
        copy_identifier=copy_on_loan.accession_number,
        returned_branch_id=b1.id,
        returned_damaged=True,
        return_notes="Loose spine stitched by conservation desk",
        actor_user_id=admin_id,
    )
    assert res_svc.get_reservation(r2.id).status == ReservationStatus.WAITING.value

    repaired_copy = books.change_copy_status(
        copy_on_loan.id,
        CopyStatus.AVAILABLE,
        note="Rebound and cleared for circulation",
        actor_user_id=admin_id,
    )
    assert repaired_copy.status == CopyStatus.RESERVED.value
    r2_ready = res_svc.get_reservation(r2.id)
    assert r2_ready.status == ReservationStatus.READY_FOR_PICKUP.value
    assert r2_ready.allocated_copy_id == repaired_copy.id


def test_regression_update_book_blocks_duplicate_non_isbn_edition(db_env):
    """Updating a catalog record to collide with an existing non-ISBN book must raise DuplicateRecordError."""
    books = db_env["books"]
    admin_id = db_env["admin_user"].id

    b_heritage_1 = books.create_book(
        title="Dastan-e-Awadh Pamphlet",
        isbn=None,
        publisher="Naval Kishore Press",
        publication_year=1912,
        edition="First",
        language="Urdu",
        category="Literature",
        author_names=["Mir Baqar Ali"],
        actor_user_id=admin_id,
    )
    b_heritage_2 = books.create_book(
        title="Distinct Monograph",
        isbn=None,
        publisher="Naval Kishore Press",
        publication_year=1920,
        edition="Second",
        language="Urdu",
        category="Literature",
        author_names=["Mir Baqar Ali"],
        actor_user_id=admin_id,
    )

    # Updating b_heritage_1 in place with its own identity must still succeed
    updated_self = books.update_book(
        b_heritage_1.id,
        title="Dastan-e-Awadh Pamphlet",
        isbn=None,
        publisher="Naval Kishore Press",
        publication_year=1912,
        edition="First",
        language="Urdu",
        category="Literature",
        author_names=["Mir Baqar Ali"],
        description="Updated archival catalog note",
        actor_user_id=admin_id,
    )
    assert updated_self.description == "Updated archival catalog note"

    # Colliding b_heritage_2 onto b_heritage_1's exact non-ISBN tuple must fail atomically
    with pytest.raises(DuplicateRecordError):
        books.update_book(
            b_heritage_2.id,
            title="  dastan-e-awadh pamphlet ",
            isbn="",
            publisher="Naval Kishore Press",
            publication_year=1912,
            edition="First",
            language="Urdu",
            category="Literature",
            author_names=["Mir Baqar Ali"],
            actor_user_id=admin_id,
        )

    unchanged = books.get_book(b_heritage_2.id)
    assert unchanged.title == "Distinct Monograph"
    assert unchanged.publication_year == 1920


def test_regression_admin_corrections_guards_and_ist_checkout_date(db_env):
    """Admin corrections must block active-loan/AVAILABLE reinstatement, accept string refs, and use IST date."""
    admin_svc = db_env["admin"]
    books = db_env["books"]
    members = db_env["members"]
    circ = db_env["circulation"]
    b1 = db_env["branches"][0]
    admin_id = db_env["admin_user"].id

    book = books.create_book(
        title="Gomti Riverfront Ecology",
        isbn="978-0-13-110362-7",
        publisher="Oxford India",
        publication_year=2019,
        category="Science",
        author_names=["Dr. K. P. Singh"],
        actor_user_id=admin_id,
    )
    copy = books.add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-CORR01",
        actor_user_id=admin_id,
    )
    member = members.register_member(
        full_name="Meera Joshi",
        email="meera.corr@test.local",
        phone="9876502001",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        actor_user_id=admin_id,
    )

    # Cannot "reinstate" an already AVAILABLE copy
    with pytest.raises(InvalidStateTransitionError):
        admin_svc.admin_reinstate_withdrawn_copy(
            copy.accession_number,
            reason="Mistakenly typed shelf copy",
            actor_user_id=admin_id,
        )

    # Issue at 2026-10-23 20:00:00 UTC == 2026-10-24 01:30:00 IST (local checkout date is 2026-10-24!)
    checkout_utc = datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc)
    loan = circ.issue_copy(
        member_identifier=member.member_code,
        copy_identifier=copy.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=admin_id,
        checkout_at=checkout_utc,
    )

    # Cannot reinstate an ON_LOAN copy while its loan is ACTIVE
    with pytest.raises(InvalidStateTransitionError):
        admin_svc.admin_reinstate_withdrawn_copy(
            copy.accession_number,
            reason="Attempted override while loan still active",
            actor_user_id=admin_id,
        )

    # Adjusting due date to 2026-10-23 (UTC date, but 1 day BEFORE IST local checkout date 2026-10-24) must fail!
    with pytest.raises(ValueError, match="cannot precede checkout date"):
        admin_svc.admin_adjust_loan_due_date(
            loan.loan_number,
            new_due_date=date(2026, 10, 23),
            reason="Invalid pre-checkout IST date",
            actor_user_id=admin_id,
        )

    # Adjusting due date by human-readable loan_number to >= 2026-10-24 succeeds
    adjusted = admin_svc.admin_adjust_loan_due_date(
        loan.loan_number,
        new_due_date=date(2026, 11, 20),
        reason="Approved archival research loan extension",
        actor_user_id=admin_id,
    )
    assert adjusted.due_date == date(2026, 11, 20)


def test_security_headers_cookie_flags_and_open_redirect_hardening(
    web_app, db_env, monkeypatch
):
    """Verify security headers, production cookie config, and safe redirect/referrer guards."""
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    prod_cfg = AppConfig(
        secret_key="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )
    assert prod_cfg.SESSION_COOKIE_SECURE is True

    monkeypatch.setenv("FLASK_ENV", "development")
    dev_cfg = AppConfig(secret_key="dev-test-secret-0123456789abcdef")
    assert dev_cfg.SESSION_COOKIE_SECURE is False

    # Direct unit verification of redirect validator edge cases
    assert is_safe_redirect_target("/books?q=awadh") is True
    assert is_safe_redirect_target("https://evil.example/phish") is False
    assert is_safe_redirect_target("//evil.example/phish") is False
    assert is_safe_redirect_target("/\\evil.example/phish") is False
    assert is_safe_redirect_target("/books\r\nSet-Cookie: bad=1") is False

    client = web_app.test_client()
    # Login POST with malicious next target must ignore external target and land on '/'
    login_page = client.get("/auth/login")
    token = _extract_csrf(login_page.get_data(as_text=True))
    login_post = client.post(
        "/auth/login",
        data={
            "username": "admin",
            "password": "AdminTestPassword!99",
            "next": "/\\evil.example/steal",
            "csrf_token": token,
        },
    )
    assert login_post.status_code == 302
    assert login_post.headers["Location"].endswith("/")

    dash = client.get("/")
    assert dash.status_code == 200
    assert dash.headers.get("X-Content-Type-Options") == "nosniff"
    assert dash.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert dash.headers.get("Cache-Control") == "no-store"

    # Forged external Referer on copy status POST must NOT redirect to evil.example
    b1 = db_env["branches"][0]
    book = db_env["books"].create_book(
        title="Redirect Guard Test Book",
        isbn="978-0-262-03384-8",
        publisher="MIT Press",
        publication_year=2009,
        category="Computer Science",
        author_names=["Thomas Cormen"],
        actor_user_id=db_env["admin_user"].id,
    )
    copy = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-REF01",
        actor_user_id=db_env["admin_user"].id,
    )
    copies_html = client.get("/copies").get_data(as_text=True)
    csrf_tok = _extract_csrf(copies_html)
    status_resp = client.post(
        f"/copies/{copy.id}/status",
        data={"new_status": "DAMAGED", "note": "Torn jacket", "csrf_token": csrf_tok},
        headers={"Referer": "https://evil.example/phish"},
    )
    assert status_resp.status_code == 302
    assert "evil.example" not in status_resp.headers["Location"]
    assert status_resp.headers["Location"].endswith("/copies")


def test_dom_unique_ids_and_label_associations_across_all_views(web_app, db_env):
    """Every rendered HTML page must have zero duplicate element IDs and valid <label for> targets."""
    admin_id = db_env["admin_user"].id
    b1 = db_env["branches"][0]
    book = db_env["books"].create_book(
        title="DOM Audit Reference Volume",
        isbn="978-0-306-40615-7",
        publisher="Rajkamal Prakashan",
        publication_year=2015,
        category="Hindi Literature",
        author_names=["Amritlal Nagar"],
        actor_user_id=admin_id,
    )
    copy1 = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-DOM01",
        actor_user_id=admin_id,
    )
    copy2 = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-DOM02",
        actor_user_id=admin_id,
    )
    m1 = db_env["members"].register_member(
        full_name="Sana Rizvi",
        email="sana.dom@test.local",
        phone="9876503001",
        tier_code="BASIC",
        home_branch_id=b1.id,
        actor_user_id=admin_id,
    )
    m2 = db_env["members"].register_member(
        full_name="Dev Mishra",
        email="dev.dom@test.local",
        phone="9876503002",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        actor_user_id=admin_id,
    )
    # Create an overdue returned loan with an unpaid fine + active loans + a reservation so loop rows render!
    old_checkout = datetime.now(timezone.utc) - timedelta(days=30)
    overdue_loan = db_env["circulation"].issue_copy(
        member_identifier=m1.member_code,
        copy_identifier=copy1.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=admin_id,
        checkout_at=old_checkout,
    )
    ret_result = db_env["circulation"].return_copy(
        loan_number=overdue_loan.loan_number,
        returned_branch_id=b1.id,
        actor_user_id=admin_id,
        returned_at=datetime.now(timezone.utc),
    )
    fine = ret_result.fine
    assert fine is not None
    db_env["circulation"].issue_copy(
        member_identifier=m2.member_code,
        copy_identifier=copy2.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=admin_id,
    )
    m3 = db_env["members"].register_member(
        full_name="Farah Zaidi",
        email="farah.dom@test.local",
        phone="9876503003",
        tier_code="PREMIUM",
        home_branch_id=b1.id,
        actor_user_id=admin_id,
    )
    m4 = db_env["members"].register_member(
        full_name="Raghav Tandon",
        email="raghav.dom@test.local",
        phone="9876503004",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        actor_user_id=admin_id,
    )
    db_env["circulation"].issue_copy(
        member_identifier=m3.member_code,
        copy_identifier=copy1.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=admin_id,
    )
    db_env["reservations"].create_reservation(
        member_identifier=m4.member_code,
        book_id=book.id,
        preferred_branch_id=b1.id,
        actor_user_id=admin_id,
    )
    db_env["admin"].create_holiday(
        holiday_date=date(2026, 12, 25),
        name="Christmas Closure",
        actor_user_id=admin_id,
    )

    client = web_app.test_client()
    # Public login view
    login_html = client.get("/auth/login").get_data(as_text=True)
    login_dom = _DOMAccessibilityParser()
    login_dom.feed(login_html)
    assert len(login_dom.ids) == len(set(login_dom.ids))
    assert set(login_dom.label_fors).issubset(set(login_dom.ids))

    _login(client, "admin", "AdminTestPassword!99")

    authenticated_paths = [
        "/",
        "/books",
        "/books/new",
        f"/books/{book.id}",
        f"/books/{book.id}/edit",
        "/books/authors",
        "/copies",
        "/members",
        "/members/new",
        f"/members/{m1.id}",
        f"/members/{m1.id}/edit",
        "/members/tiers",
        "/circulation",
        "/circulation?status=RETURNED",
        "/reservations",
        "/fines",
        f"/fines/{fine.id}",
        "/fines/payments",
        "/reports?type=overdue",
        "/reports?type=inventory",
        "/data-quality",
        "/admin",
        "/admin/users",
        "/admin/branches",
        "/admin/tiers",
        "/admin/holidays",
        "/admin/corrections",
        "/admin/audit",
        "/settings",
    ]
    for path in authenticated_paths:
        resp = client.get(path)
        assert resp.status_code == 200, f"GET {path} returned {resp.status_code}"
        parser = _DOMAccessibilityParser()
        parser.feed(resp.get_data(as_text=True))
        duplicates = [i for i in set(parser.ids) if parser.ids.count(i) > 1]
        assert not duplicates, f"Duplicate HTML IDs on {path}: {duplicates}"
        missing_targets = [
            target for target in parser.label_fors if target not in set(parser.ids)
        ]
        assert not missing_targets, (
            f"Broken <label for> targets on {path}: {missing_targets}"
        )
        assert "#main-content" in parser.skip_links, f"Missing skip-link on {path}"
        assert "main-content" in parser.ids, f"Missing #main-content landmark on {path}"


def test_all_twelve_web_workflows_persist_on_success_and_preserve_db_on_failure(
    tmp_path: Path,
):
    """End-to-end verification of Web Workflows 1–12 on an isolated Alembic-migrated database."""
    db_file = tmp_path / "workflows_pustakalaya.db"
    db_url = f"sqlite:///{db_file.as_posix()}"
    hist_path = tmp_path / "historical_web_fixture.json"

    alembic_cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(alembic_cfg, "head")

    cfg = AppConfig(
        secret_key="workflow-suite-secret-key-0123456789abcdef",
        database_url=db_url,
        testing=True,
        csrf_enabled=True,
        historical_source_path=hist_path,
    )
    app = create_app(cfg)
    sf = app.extensions["session_factory"]

    try:
        client = app.test_client()

        # --- Workflow 1: First-run admin setup + lock-down + invalid/valid login ---
        setup_get = client.get("/auth/setup")
        assert setup_get.status_code == 200
        setup_dom = _DOMAccessibilityParser()
        setup_dom.feed(setup_get.get_data(as_text=True))
        assert len(setup_dom.ids) == len(set(setup_dom.ids))
        assert set(setup_dom.label_fors).issubset(set(setup_dom.ids))

        setup_csrf = _extract_csrf(setup_get.get_data(as_text=True))
        # Failure case: mismatched confirm_password -> zero users created
        bad_setup = client.post(
            "/auth/setup",
            data={
                "csrf_token": setup_csrf,
                "username": "chiefadmin",
                "full_name": "Dr. Meenakshi Awasthi",
                "email": "chief@pustakalaya.test",
                "password": "StrongAdminPassword!1",
                "confirm_password": "DifferentPassword!2",
            },
        )
        assert bad_setup.status_code == 200
        assert app.extensions["services"]["auth"].count_users() == 0

        # Success case: creates hashed ADMIN account and signs in
        ok_setup = client.post(
            "/auth/setup",
            data={
                "csrf_token": setup_csrf,
                "username": "chiefadmin",
                "full_name": "Dr. Meenakshi Awasthi",
                "email": "chief@pustakalaya.test",
                "password": "StrongAdminPassword!1",
                "confirm_password": "StrongAdminPassword!1",
            },
            follow_redirects=True,
        )
        assert ok_setup.status_code == 200
        assert app.extensions["services"]["auth"].count_users() == 1

        # Subsequent POST to /auth/setup with a valid session CSRF token is locked with HTTP 403
        post_setup_csrf = _extract_csrf(ok_setup.get_data(as_text=True))
        assert (
            client.post(
                "/auth/setup",
                data={"csrf_token": post_setup_csrf, "username": "attacker"},
            ).status_code
            == 403
        )

        # Sign out, verify bad password fails without session, then sign back in
        client.post("/auth/logout", data={"csrf_token": post_setup_csrf})
        bad_login = _login(client, "chiefadmin", "WrongPassword!000")
        assert "Invalid username or password" in bad_login.get_data(as_text=True)
        assert client.get("/").status_code == 302  # still unauthenticated
        good_login = _login(client, "chiefadmin", "StrongAdminPassword!1")
        assert good_login.status_code == 200

        # Seed branches & tiers so all branch/tier UI selectors are populated
        with write_transaction(sf) as session:
            session.add_all(
                [
                    MembershipTier(
                        code="BASIC",
                        name="Basic Citizen",
                        description="Basic",
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
                        description="Standard",
                        max_active_loans=5,
                        loan_duration_days=21,
                        max_renewals=1,
                        renewal_duration_days=14,
                        max_active_reservations=4,
                        grace_period_days=2,
                        daily_fine_rate=Decimal("3.00"),
                        max_fine_per_loan=Decimal("200.00"),
                        fine_blocking_threshold=Decimal("100.00"),
                        borrowing_enabled=True,
                    ),
                ]
            )

        br_tok = _extract_csrf(client.get("/admin/branches").get_data(as_text=True))
        client.post(
            "/admin/branches",
            data={
                "csrf_token": br_tok,
                "code": "LKO-HZR",
                "name": "Hazratganj Central",
                "address": "MG Marg, Hazratganj",
                "pincode": "226001",
                "phone": "+91-522-2620101",
                "email": "hzr@pustakalaya.test",
                "is_active": "y",
            },
        )
        client.post(
            "/admin/branches",
            data={
                "csrf_token": br_tok,
                "code": "LKO-GMN",
                "name": "Gomti Nagar Branch",
                "address": "Vipul Khand, Gomti Nagar",
                "pincode": "226010",
                "phone": "+91-522-2304410",
                "email": "gmn@pustakalaya.test",
                "is_active": "y",
            },
        )
        with read_session(sf) as s:
            hzr_id = s.scalar(
                select(LibraryBranch.id).where(LibraryBranch.code == "LKO-HZR")
            )
            gmn_id = s.scalar(
                select(LibraryBranch.id).where(LibraryBranch.code == "LKO-GMN")
            )
            assert hzr_id is not None and gmn_id is not None

        # Verify Branch Edit regression fix (POST /admin/branches/<id>/edit without 'code')
        edit_br = client.post(
            f"/admin/branches/{hzr_id}/edit",
            data={
                "csrf_token": br_tok,
                "name": "Hazratganj Central Library",
                "address": "74 MG Marg, Hazratganj",
                "pincode": "226001",
                "phone": "+91-522-2620199",
                "email": "hzr@pustakalaya.test",
                "is_active": "y",
            },
            follow_redirects=True,
        )
        assert "Updated branch" in edit_br.get_data(as_text=True)
        with read_session(sf) as s:
            assert s.get(LibraryBranch, hzr_id).name == "Hazratganj Central Library"

        # --- Workflow 2: Create book & add copy (plus invalid ISBN failure check) ---
        bk_tok = _extract_csrf(client.get("/books/new").get_data(as_text=True))
        bad_book = client.post(
            "/books/new",
            data={
                "csrf_token": bk_tok,
                "title": "Broken Checksum Book",
                "isbn": "978-0-306-40615-9",  # invalid check digit
                "publisher": "Penguin India",
                "publication_year": "2022",
                "language": "English",
                "category": "History",
                "author_names": "Veena Talwar Oldenburg",
            },
            follow_redirects=True,
        )
        assert "ISBN" in bad_book.get_data(as_text=True)
        with read_session(sf) as s:
            assert s.scalar(select(func.count(Book.id))) == 0

        ok_book = client.post(
            "/books/new",
            data={
                "csrf_token": bk_tok,
                "title": "Sham-e-Awadh Chronicles",
                "isbn": "978-0-306-40615-7",
                "publisher": "Penguin India",
                "publication_year": "2022",
                "language": "English",
                "category": "History",
                "author_names": "Veena Talwar Oldenburg",
            },
            follow_redirects=True,
        )
        assert ok_book.status_code == 200
        with read_session(sf) as s:
            book_id = s.scalar(select(Book.id))
            assert book_id is not None

        cp_tok = _extract_csrf(client.get("/copies").get_data(as_text=True))
        client.post(
            "/copies/new",
            data={
                "csrf_token": cp_tok,
                "book_id": str(book_id),
                "owning_branch_id": str(hzr_id),
                "accession_number": "LKO-HZR-WF0001",
                "acquisition_date": "2026-01-15",
                "status": "AVAILABLE",
                "condition_notes": "New stitched hardcover",
            },
            follow_redirects=True,
        )
        with read_session(sf) as s:
            assert s.scalar(select(func.count(BookCopy.id))) == 1

        # --- Workflow 3: Register members (plus duplicate email failure check) ---
        mem_tok = _extract_csrf(client.get("/members/new").get_data(as_text=True))
        client.post(
            "/members/new",
            data={
                "csrf_token": mem_tok,
                "full_name": "Aarav Rastogi",
                "email": "aarav.wf@test.local",
                "phone": "9876540001",
                "address": "Hazratganj, Lucknow",
                "tier_code": "BASIC",
                "home_branch_id": str(hzr_id),
                "expiry_date": "2027-12-31",
            },
            follow_redirects=True,
        )
        # Duplicate email fails and leaves count at 1
        dup_mem = client.post(
            "/members/new",
            data={
                "csrf_token": mem_tok,
                "full_name": "Duplicate User",
                "email": "AARAV.WF@test.local",
                "phone": "9876540099",
                "address": "Hazratganj, Lucknow",
                "tier_code": "BASIC",
                "home_branch_id": str(hzr_id),
                "expiry_date": "2027-12-31",
            },
            follow_redirects=True,
        )
        assert "already" in dup_mem.get_data(as_text=True).lower()
        with read_session(sf) as s:
            assert s.scalar(select(func.count(Member.id))) == 1

        client.post(
            "/members/new",
            data={
                "csrf_token": mem_tok,
                "full_name": "Zoya Husain",
                "email": "zoya.wf@test.local",
                "phone": "9876540002",
                "address": "Gomti Nagar, Lucknow",
                "tier_code": "STANDARD",
                "home_branch_id": str(hzr_id),
                "expiry_date": "2027-12-31",
            },
            follow_redirects=True,
        )
        with read_session(sf) as s:
            members_list = list(s.scalars(select(Member).order_by(Member.id.asc())))
            assert len(members_list) == 2
            m1_code, m2_code = members_list[0].member_code, members_list[1].member_code

        # --- Workflow 4: Issue available copy (plus double-issue failure check) ---
        circ_tok = _extract_csrf(client.get("/circulation").get_data(as_text=True))
        issue_ok = client.post(
            "/circulation/issue",
            data={
                "csrf_token": circ_tok,
                "member_identifier": m1_code,
                "copy_identifier": "LKO-HZR-WF0001",
                "issuing_branch_id": str(hzr_id),
            },
            follow_redirects=True,
        )
        assert "Issued copy" in issue_ok.get_data(as_text=True)

        issue_dup = client.post(
            "/circulation/issue",
            data={
                "csrf_token": circ_tok,
                "member_identifier": m2_code,
                "copy_identifier": "LKO-HZR-WF0001",
                "issuing_branch_id": str(hzr_id),
            },
            follow_redirects=True,
        )
        assert "ON_LOAN" in issue_dup.get_data(as_text=True)
        with read_session(sf) as s:
            assert s.scalar(select(func.count(Loan.id))) == 1
            active_loan = s.scalar(select(Loan))
            loan_id = active_loan.id
            loan_number = active_loan.loan_number

        # --- Workflow 7: Renew eligible loan, then block exceeding max_renewals (BASIC max_renewals=1) ---
        renew_ok = client.post(
            f"/circulation/loans/{loan_id}/renew",
            data={"csrf_token": circ_tok},
            follow_redirects=True,
        )
        assert "Renewed loan" in renew_ok.get_data(as_text=True)
        with read_session(sf) as s:
            due_after_1 = s.get(Loan, loan_id).due_date
            assert s.get(Loan, loan_id).renewal_count == 1

        renew_cap = client.post(
            f"/circulation/loans/{loan_id}/renew",
            data={"csrf_token": circ_tok},
            follow_redirects=True,
        )
        assert "renewal" in renew_cap.get_data(as_text=True).lower()
        with read_session(sf) as s:
            assert s.get(Loan, loan_id).renewal_count == 1
            assert s.get(Loan, loan_id).due_date == due_after_1

        # --- Workflow 8: Create reservation while copy ON_LOAN + cancel + re-create ---
        res_tok = _extract_csrf(client.get("/reservations").get_data(as_text=True))
        res_create = client.post(
            "/reservations/new",
            data={
                "csrf_token": res_tok,
                "member_identifier": m2_code,
                "book_id": str(book_id),
                "preferred_branch_id": str(hzr_id),
            },
            follow_redirects=True,
        )
        assert "reservation" in res_create.get_data(as_text=True).lower()
        with read_session(sf) as s:
            res_obj = s.scalar(select(Reservation))
            assert res_obj.status == ReservationStatus.WAITING.value
            res_1_id = res_obj.id

        res_cancel = client.post(
            f"/reservations/{res_1_id}/cancel",
            data={"csrf_token": res_tok, "reason": "Patron requested pause"},
            follow_redirects=True,
        )
        assert "Cancelled reservation" in res_cancel.get_data(as_text=True)
        with read_session(sf) as s:
            assert s.get(Reservation, res_1_id).status == ReservationStatus.CANCELLED.value

        # Create active FIFO hold again for Workflow 9 return-allocation test
        client.post(
            "/reservations/new",
            data={
                "csrf_token": res_tok,
                "member_identifier": m2_code,
                "book_id": str(book_id),
                "preferred_branch_id": str(hzr_id),
            },
            follow_redirects=True,
        )

        # Backdate active loan so Workflow 10 (Overdue Report) & Workflow 5 (Fine Assessment) run on real overdue data!
        with write_transaction(sf) as s:
            ln = s.get(Loan, loan_id)
            ln.checked_out_at = datetime.now(timezone.utc) - timedelta(days=25)
            ln.due_date = date(2026, 10, 18)  # 6 days before 2026-10-24 (or local_today - 6)
            ln.due_date = (
                datetime.now(timezone.utc).astimezone().date() - timedelta(days=6)
            )

        # --- Workflow 10: Branch overdue report (HTML + CSV) ---
        rep_hzr = client.get(f"/reports?type=overdue&branch_id={hzr_id}")
        assert rep_hzr.status_code == 200
        assert loan_number in rep_hzr.get_data(as_text=True)
        rep_gmn = client.get(f"/reports?type=overdue&branch_id={gmn_id}")
        assert loan_number not in rep_gmn.get_data(as_text=True)
        csv_hzr = client.get(f"/reports/overdue.csv?branch_id={hzr_id}&mode=active")
        assert csv_hzr.status_code == 200
        assert loan_number in csv_hzr.get_data(as_text=True)

        # --- Workflow 5 & Workflow 9: Return overdue copy -> Fine assessed + FIFO hold allocated! ---
        ret_resp = client.post(
            "/circulation/return",
            data={
                "csrf_token": circ_tok,
                "identifier": "LKO-HZR-WF0001",
                "returned_branch_id": str(hzr_id),
                "return_notes": "Returned intact at Hazratganj desk",
            },
            follow_redirects=True,
        )
        assert "Returned" in ret_resp.get_data(as_text=True)
        with read_session(sf) as s:
            closed_loan = s.get(Loan, loan_id)
            assert closed_loan.status == LoanStatus.RETURNED.value
            assessed_fine = s.scalar(select(Fine).where(Fine.loan_id == loan_id))
            assert assessed_fine is not None
            assert assessed_fine.assessed_amount > Decimal("0.00")
            expected_amt = assessed_fine.assessed_amount
            fine_id = assessed_fine.id
            copy_row = s.scalar(select(BookCopy))
            assert copy_row.status == CopyStatus.RESERVED.value
            ready_res = s.scalar(
                select(Reservation).where(
                    Reservation.status == ReservationStatus.READY_FOR_PICKUP.value
                )
            )
            assert ready_res is not None
            assert ready_res.allocated_copy_id == copy_row.id

        # Non-reserving member m1 cannot check out RESERVED copy; reserving member m2 fulfills hold!
        blocked_issue = client.post(
            "/circulation/issue",
            data={
                "csrf_token": circ_tok,
                "member_identifier": m1_code,
                "copy_identifier": "LKO-HZR-WF0001",
                "issuing_branch_id": str(hzr_id),
            },
            follow_redirects=True,
        )
        assert "reserved" in blocked_issue.get_data(as_text=True).lower()

        fulfill_issue = client.post(
            "/circulation/issue",
            data={
                "csrf_token": circ_tok,
                "member_identifier": m2_code,
                "copy_identifier": "LKO-HZR-WF0001",
                "issuing_branch_id": str(hzr_id),
            },
            follow_redirects=True,
        )
        assert "Issued copy" in fulfill_issue.get_data(as_text=True)
        with read_session(sf) as s:
            assert (
                s.get(Reservation, ready_res.id).status
                == ReservationStatus.FULFILLED.value
            )

        # --- Workflow 6: Partial & full fine payments + overpayment failure check ---
        fine_page = client.get(f"/fines/{fine_id}").get_data(as_text=True)
        fine_tok = _extract_csrf(fine_page)

        overpay = client.post(
            f"/fines/{fine_id}/pay",
            data={
                "csrf_token": fine_tok,
                "amount": "999.00",
                "payment_method": PaymentMethod.CASH.value,
            },
            follow_redirects=True,
        )
        assert "exceed" in overpay.get_data(as_text=True).lower()
        with read_session(sf) as s:
            assert s.get(Fine, fine_id).paid_amount == Decimal("0.00")
            assert s.scalar(select(func.count(FinePayment.id))) == 0

        partial_amt = Decimal("5.00")
        remaining_amt = expected_amt - partial_amt
        client.post(
            f"/fines/{fine_id}/pay",
            data={
                "csrf_token": fine_tok,
                "amount": f"{partial_amt:.2f}",
                "payment_method": PaymentMethod.UPI.value,
                "reference_number": "UPI-PARTIAL-1",
            },
            follow_redirects=True,
        )
        with read_session(sf) as s:
            f_mid = s.get(Fine, fine_id)
            assert f_mid.status == FineStatus.PARTIALLY_PAID.value
            assert f_mid.paid_amount == partial_amt
            assert f_mid.assessed_amount == expected_amt

        fine_tok_2 = _extract_csrf(
            client.get(f"/fines/{fine_id}").get_data(as_text=True)
        )
        client.post(
            f"/fines/{fine_id}/pay",
            data={
                "csrf_token": fine_tok_2,
                "amount": f"{remaining_amt:.2f}",
                "payment_method": PaymentMethod.CASH.value,
            },
            follow_redirects=True,
        )
        with read_session(sf) as s:
            f_paid = s.get(Fine, fine_id)
            assert f_paid.status == FineStatus.PAID.value
            assert f_paid.paid_amount == expected_amt
            assert f_paid.assessed_amount == expected_amt
            assert s.scalar(select(func.count(FinePayment.id))) == 2

        # --- Workflow 11: Historical import, batch breakdown selection, & idempotency ---
        hist_records = [
            {
                "source_id": "HIST-WF-GOOD-BOOK",
                "record_type": "BOOK",
                "title": "Tarikh-e-Lucknow",
                "isbn": "978-0-13-110362-7",
                "publisher": "Kitab Ghar",
                "publication_year": 2018,
                "language": "Urdu",
                "category": "History",
                "authors": ["Abdul Halim Sharar"],
            },
            {
                "source_id": "HIST-WF-BAD-ISBN",
                "record_type": "BOOK",
                "title": "Corrupt ISBN Legacy Entry",
                "isbn": "978-0-13-110362-9",
                "publisher": "Legacy",
                "publication_year": 2010,
                "category": "History",
                "authors": ["Unknown"],
            },
            {
                "source_id": "HIST-WF-NO-RET",
                "record_type": "LOAN",
                "member_code": m1_code,
                "copy_accession": "LKO-HZR-WF0001",
                "branch_code": "LKO-HZR",
                "checkout_date": "2025-12-01",
                "due_date": "2025-12-15",
                "return_date": None,
                "status": "RETURNED",
            },
        ]
        hist_path.write_text(json.dumps(hist_records), encoding="utf-8")

        dq_tok = _extract_csrf(client.get("/data-quality").get_data(as_text=True))
        imp1 = client.post(
            "/data-quality/run-import",
            data={"csrf_token": dq_tok},
            follow_redirects=True,
        )
        assert imp1.status_code == 200
        assert "1 accepted" in imp1.get_data(as_text=True)
        assert "MALFORMED_ISBN" in imp1.get_data(as_text=True)

        imp2 = client.post(
            "/data-quality/run-import",
            data={"csrf_token": dq_tok},
            follow_redirects=True,
        )
        assert imp2.status_code == 200
        assert "0 accepted" in imp2.get_data(as_text=True)

        # Verify batch-scoped breakdown regression fix (?batch_id=1 vs ?batch_id=2)
        dq_b1_html = client.get("/data-quality?batch_id=1").get_data(as_text=True)
        assert "MALFORMED_ISBN" in dq_b1_html
        assert "MISSING_RETURN_DATE" in dq_b1_html
        dq_b2_html = client.get("/data-quality?batch_id=2").get_data(as_text=True)
        assert "DUPLICATE_CATALOG_ISBN" in dq_b2_html or "MALFORMED_ISBN" in dq_b2_html

        # --- Workflow 12: Provision LIBRARIAN & STAFF_READONLY via /admin/users and verify RBAC ---
        usr_tok = _extract_csrf(client.get("/admin/users").get_data(as_text=True))
        client.post(
            "/admin/users",
            data={
                "csrf_token": usr_tok,
                "username": "desk_lib",
                "full_name": "Desk Librarian",
                "email": "lib.wf@test.local",
                "role": "LIBRARIAN",
                "branch_id": str(hzr_id),
                "password": "LibrarianPassword!1",
            },
            follow_redirects=True,
        )
        client.post(
            "/admin/users",
            data={
                "csrf_token": usr_tok,
                "username": "viewer_staff",
                "full_name": "Readonly Clerk",
                "email": "ro.wf@test.local",
                "role": "STAFF_READONLY",
                "branch_id": str(hzr_id),
                "password": "ReadonlyPassword!1",
            },
            follow_redirects=True,
        )

        # Switch to LIBRARIAN: can access /members/new, blocked from /admin, /books/new, /data-quality/import
        client.post("/auth/logout", data={"csrf_token": usr_tok})
        _login(client, "desk_lib", "LibrarianPassword!1")
        assert client.get("/members/new").status_code == 200
        assert client.get("/admin").status_code == 403
        assert client.get("/books/new").status_code == 403
        lib_tok = _extract_csrf(client.get("/settings").get_data(as_text=True))
        assert (
            client.post(
                "/data-quality/run-import", data={"csrf_token": lib_tok}
            ).status_code
            == 403
        )

        # Switch to STAFF_READONLY: can view /reports, blocked from /members/new and /circulation/issue
        client.post("/auth/logout", data={"csrf_token": lib_tok})
        _login(client, "viewer_staff", "ReadonlyPassword!1")
        assert client.get("/reports").status_code == 200
        ro_tok = _extract_csrf(client.get("/settings").get_data(as_text=True))
        assert (
            client.post(
                "/members/new",
                data={"csrf_token": ro_tok, "full_name": "Nope"},
            ).status_code
            == 403
        )
        with read_session(sf) as s:
            assert s.scalar(select(func.count(Member.id))) == 2
    finally:
        app.extensions["db_engine"].dispose()


def test_verify_data_invariants_pass_on_isolated_seeded_and_imported_db(tmp_path: Path):
    """Run Alembic migration twice, seed_operational_dataset twice, import twice, and verify_data on isolated DB."""
    db_file = tmp_path / "isolated_full_verify.db"
    db_url = f"sqlite:///{db_file.as_posix()}"
    hist_file = tmp_path / "isolated_historical.json"

    alembic_cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(alembic_cfg, "head")
    command.upgrade(alembic_cfg, "head")  # idempotent migration check

    app_inst = create_app(
        AppConfig(
            secret_key="isolated-verify-secret-0123456789abcdef",
            database_url=db_url,
            testing=True,
            historical_source_path=hist_file,
        )
    )
    engine = app_inst.extensions["db_engine"]
    sf = app_inst.extensions["session_factory"]

    try:
        seed_1 = seed_operational_dataset(sf, historical_output_path=hist_file)
        seed_2 = seed_operational_dataset(sf, historical_output_path=hist_file)
        assert seed_1.books == 2000 and seed_2.books == 2000
        assert seed_1.members == 500 and seed_2.members == 500
        assert seed_1.copies == 4200 and seed_2.copies == 4200

        imp_svc = DataImportService(sf)
        b1 = imp_svc.import_from_file(hist_file)
        b2 = imp_svc.import_from_file(hist_file)
        assert b1.accepted_count == 1275
        assert b2.accepted_count == 0
        assert b2.error_count == 0

        report = run_verification(db_url)
        assert report.passed is True
        assert report.foreign_key_violations == 0
        assert all(v == 0 for v in report.invariant_violations.values())
    finally:
        engine.dispose()

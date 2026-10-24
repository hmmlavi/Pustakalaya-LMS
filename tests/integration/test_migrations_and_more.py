"""Additional integration & web tests covering Alembic migrations, edge cases, and POST flows."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import re

import pytest
from sqlalchemy import inspect

from app.domain.enums import CopyStatus, FineStatus, ReservationStatus
from app.domain.exceptions import (
    CirculationError,
    LoanAlreadyReturnedError,
    RenewalLimitExceededError,
)
from app.infrastructure.database import apply_migrations, create_db_engine


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _login(client, username: str, password: str):
    page = client.get("/auth/login")
    token = _extract_csrf(page.get_data(as_text=True))
    return client.post(
        "/auth/login",
        data={"username": username, "password": password, "csrf_token": token},
        follow_redirects=True,
    )


def test_alembic_programmatic_migrations_create_all_16_tables_idempotently(tmp_path: Path):
    mig_db = tmp_path / "alembic_test.db"
    db_url = f"sqlite:///{mig_db.as_posix()}"
    apply_migrations(db_url)
    # Second invocation must be a clean no-op
    apply_migrations(db_url)

    engine = create_db_engine(db_url)
    try:
        tables = set(inspect(engine).get_table_names())
        expected = {
            "alembic_version",
            "branches",
            "authors",
            "books",
            "book_authors",
            "book_copies",
            "membership_tiers",
            "members",
            "users",
            "loans",
            "reservations",
            "fines",
            "fine_payments",
            "library_holidays",
            "audit_events",
            "import_batches",
            "import_issues",
        }
        assert expected.issubset(tables)
    finally:
        engine.dispose()


def test_cross_branch_checkout_mismatch_rejected(db_env):
    b1, b2, _ = db_env["branches"]
    book = db_env["books"].create_book(
        title="Branch Ownership Check",
        isbn=None,
        publisher="Civic Press",
        publication_year=2022,
        category="History & Archaeology",
        author_names=["Author One"],
        actor_user_id=db_env["admin_user"].id,
    )
    copy_at_b1 = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-OWN-01",
        actor_user_id=db_env["admin_user"].id,
    )
    member = db_env["members"].register_member(
        full_name="Cross Branch Member",
        email="cbm@example.org",
        tier_code="STANDARD",
        home_branch_id=b2.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    # Issuing a copy owned by b1 while claiming issuing_branch_id=b2 is rejected
    with pytest.raises(CirculationError, match="belongs to"):
        db_env["circulation"].issue_copy(
            member_identifier=member.member_code,
            copy_identifier=copy_at_b1.accession_number,
            issuing_branch_id=b2.id,
            actor_user_id=db_env["librarian_user"].id,
        )


def test_returning_already_returned_loan_is_rejected(db_env):
    b1, _, _ = db_env["branches"]
    book = db_env["books"].create_book(
        title="Double Return Guard",
        isbn=None,
        publisher="Civic Press",
        publication_year=2022,
        category="World Fiction",
        author_names=["Author Two"],
        actor_user_id=db_env["admin_user"].id,
    )
    copy = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-DBLRET-01",
        actor_user_id=db_env["admin_user"].id,
    )
    member = db_env["members"].register_member(
        full_name="Return Guard Member",
        email="rgm@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    loan = db_env["circulation"].issue_copy(
        member_identifier=member.member_code,
        copy_identifier=copy.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    db_env["circulation"].return_copy(
        loan_id=loan.id,
        returned_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    with pytest.raises(LoanAlreadyReturnedError):
        db_env["circulation"].return_copy(
            loan_id=loan.id,
            returned_branch_id=b1.id,
            actor_user_id=db_env["librarian_user"].id,
        )


def test_loan_renewal_extends_due_date_until_tier_max_renewals_reached(db_env):
    b1, _, _ = db_env["branches"]
    book = db_env["books"].create_book(
        title="Renewal Sequence Book",
        isbn=None,
        publisher="Civic Press",
        publication_year=2023,
        category="Science & Ecology",
        author_names=["Author Three"],
        actor_user_id=db_env["admin_user"].id,
    )
    copy = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-REN-01",
        actor_user_id=db_env["admin_user"].id,
    )
    member = db_env["members"].register_member(
        full_name="Renewal Reader",
        email="ren@example.org",
        tier_code="BASIC",  # max_renewals=1, renewal_duration_days=7
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )
    t0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    loan = db_env["circulation"].issue_copy(
        member_identifier=member.member_code,
        copy_identifier=copy.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        checkout_at=t0,
    )
    assert loan.due_date == date(2026, 10, 15)

    renewed = db_env["circulation"].renew_loan(
        loan.id,
        actor_user_id=db_env["librarian_user"].id,
        renewed_at=datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc),
    )
    assert renewed.renewal_count == 1
    assert renewed.due_date == date(2026, 10, 22)

    with pytest.raises(RenewalLimitExceededError):
        db_env["circulation"].renew_loan(
            loan.id,
            actor_user_id=db_env["librarian_user"].id,
            renewed_at=datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc),
        )


def test_reserved_copy_marked_damaged_on_hold_shelf_returns_reservation_to_waiting(db_env):
    b1, _, _ = db_env["branches"]
    book = db_env["books"].create_book(
        title="Hold Shelf Damage Book",
        isbn=None,
        publisher="Civic Press",
        publication_year=2021,
        category="World Fiction",
        author_names=["Author Four"],
        actor_user_id=db_env["admin_user"].id,
    )
    copy = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-HOLDDMG-01",
        actor_user_id=db_env["admin_user"].id,
    )
    m1 = db_env["members"].register_member(
        full_name="First Borrower",
        email="fb@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    m2 = db_env["members"].register_member(
        full_name="Hold Patron",
        email="hp@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    db_env["circulation"].issue_copy(
        member_identifier=m1.member_code,
        copy_identifier=copy.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    res = db_env["reservations"].create_reservation(
        member_identifier=m2.member_code,
        book_id=book.id,
        preferred_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    db_env["circulation"].return_copy(
        copy_identifier=copy.accession_number,
        returned_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    assert (
        db_env["reservations"].get_reservation(res.id).status
        == ReservationStatus.READY_FOR_PICKUP.value
    )

    # Staff discovers water leak on hold shelf and marks copy DAMAGED
    updated_copy = db_env["books"].change_copy_status(
        copy.id,
        CopyStatus.DAMAGED,
        note="Shelf leak",
        actor_user_id=db_env["admin_user"].id,
    )
    assert updated_copy.status == CopyStatus.DAMAGED.value
    # Reservation gracefully returns to WAITING queue instead of stranding m2!
    refreshed_res = db_env["reservations"].get_reservation(res.id)
    assert refreshed_res.status == ReservationStatus.WAITING.value
    assert refreshed_res.allocated_copy_id is None


def test_web_return_fine_payment_and_waiver_post_flow(web_app, db_env):
    b1, _, _ = db_env["branches"]
    book = db_env["books"].create_book(
        title="Web Fine Settlement Book",
        isbn=None,
        publisher="Lucknow Press",
        publication_year=2020,
        category="Hindi Literature",
        author_names=["Premchand"],
        actor_user_id=db_env["admin_user"].id,
    )
    copy = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-WEBFINE-01",
        actor_user_id=db_env["admin_user"].id,
    )
    m = db_env["members"].register_member(
        full_name="Web Fine Patron",
        email="webfine@example.org",
        tier_code="BASIC",
        home_branch_id=b1.id,
        expiry_date=date(2027, 12, 31),
        actor_user_id=db_env["librarian_user"].id,
    )
    db_env["circulation"].issue_copy(
        member_identifier=m.member_code,
        copy_identifier=copy.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
        checkout_at=datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
    )

    client = web_app.test_client()
    _login(client, "admin", "AdminTestPassword!99")

    # Return overdue copy via web POST
    circ_html = client.get("/circulation").get_data(as_text=True)
    token = _extract_csrf(circ_html)
    ret_resp = client.post(
        "/circulation/return",
        data={
            "csrf_token": token,
            "identifier": copy.accession_number,
            "returned_branch_id": str(b1.id),
            "return_notes": "Returned via web test",
        },
        follow_redirects=True,
    )
    assert ret_resp.status_code == 200
    assert "Overdue fine" in ret_resp.get_data(as_text=True)

    fine = db_env["fines"].search_fines().items[0]
    fine_page = client.get(f"/fines/{fine.id}").get_data(as_text=True)
    token = _extract_csrf(fine_page)

    # Partial payment via web POST
    pay_resp = client.post(
        f"/fines/{fine.id}/pay",
        data={
            "csrf_token": token,
            "amount": "10.00",
            "payment_method": "UPI",
            "branch_id": str(b1.id),
            "reference_note": "UPI-REF-99",
            "idempotency_key": "WEB-IDEMP-99",
        },
        follow_redirects=True,
    )
    assert pay_resp.status_code == 200
    assert "Recorded payment receipt" in pay_resp.get_data(as_text=True)

    # Waive remaining via web POST
    fine_page2 = client.get(f"/fines/{fine.id}").get_data(as_text=True)
    token2 = _extract_csrf(fine_page2)
    waive_resp = client.post(
        f"/fines/{fine.id}/waive",
        data={"csrf_token": token2, "reason": "Approved administrative waiver"},
        follow_redirects=True,
    )
    assert waive_resp.status_code == 200
    assert db_env["fines"].get_fine(fine.id).status == FineStatus.PAID.value


def test_web_reservation_create_cancel_and_admin_zero_grace_tier_update(web_app, db_env):
    b1, _, _ = db_env["branches"]
    book = db_env["books"].create_book(
        title="Web Hold Book",
        isbn=None,
        publisher="Lucknow Press",
        publication_year=2022,
        category="World Fiction",
        author_names=["Author Hold"],
        actor_user_id=db_env["admin_user"].id,
    )
    copy = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=b1.id,
        accession_number="LKO-HZR-WEBHOLD-01",
        actor_user_id=db_env["admin_user"].id,
    )
    m1 = db_env["members"].register_member(
        full_name="Borrower Web",
        email="bw@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    m2 = db_env["members"].register_member(
        full_name="Waiter Web",
        email="ww@example.org",
        tier_code="STANDARD",
        home_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )
    db_env["circulation"].issue_copy(
        member_identifier=m1.member_code,
        copy_identifier=copy.accession_number,
        issuing_branch_id=b1.id,
        actor_user_id=db_env["librarian_user"].id,
    )

    client = web_app.test_client()
    _login(client, "admin", "AdminTestPassword!99")

    # Place reservation via web POST
    res_html = client.get("/reservations").get_data(as_text=True)
    token = _extract_csrf(res_html)
    res_post = client.post(
        "/reservations/new",
        data={
            "csrf_token": token,
            "member_identifier": m2.member_code,
            "book_id": str(book.id),
            "preferred_branch_id": str(b1.id),
            "notes": "Hold for research",
        },
        follow_redirects=True,
    )
    assert res_post.status_code == 200
    assert "FIFO queue position #1" in res_post.get_data(as_text=True)

    # Cancel reservation via web POST
    res_obj = db_env["reservations"].search_reservations()[0].items[0]
    res_html2 = client.get("/reservations").get_data(as_text=True)
    token2 = _extract_csrf(res_html2)
    cancel_post = client.post(
        f"/reservations/{res_obj.id}/cancel",
        data={"csrf_token": token2, "reason": "No longer needed"},
        follow_redirects=True,
    )
    assert cancel_post.status_code == 200
    assert (
        db_env["reservations"].get_reservation(res_obj.id).status
        == ReservationStatus.CANCELLED.value
    )

    # Admin updates BASIC tier with grace_period_days = 0 (InputRequired supports 0!)
    tiers_html = client.get("/admin/tiers").get_data(as_text=True)
    token3 = _extract_csrf(tiers_html)
    tier_post = client.post(
        "/admin/tiers/BASIC",
        data={
            "csrf_token": token3,
            "max_active_loans": "3",
            "loan_duration_days": "14",
            "max_renewals": "0",
            "renewal_duration_days": "7",
            "max_active_reservations": "2",
            "grace_period_days": "0",
            "daily_fine_rate": "5.00",
            "max_fine_per_loan": "150.00",
            "fine_blocking_threshold": "50.00",
            "borrowing_enabled": "y",
        },
        follow_redirects=True,
    )
    assert tier_post.status_code == 200
    basic_tier = [t for t in db_env["admin"].list_tiers() if t.code == "BASIC"][0]
    assert basic_tier.grace_period_days == 0
    assert basic_tier.max_renewals == 0


def test_web_data_quality_run_import_and_settings_password_change(web_app):
    # Write a tiny valid + invalid fixture at web_app's HISTORICAL_SOURCE_PATH
    src_path: Path = web_app.config["HISTORICAL_SOURCE_PATH"]
    src_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "source_id": "WEB-IMP-BOOK-1",
                        "record_type": "BOOK",
                        "title": "Imported Lucknow Gazetteer",
                        "isbn": "9780140449136",
                        "publisher": "Government Press",
                        "publication_year": 1959,
                        "category": "Lucknow & Awadh Heritage",
                        "authors": ["William Crooke"],
                    },
                    {
                        "source_id": "WEB-IMP-BAD-1",
                        "record_type": "BOOK",
                        "title": "Broken Checksum",
                        "isbn": "9780140449130",
                        "publisher": "Government Press",
                        "publication_year": 1959,
                        "category": "History",
                        "authors": ["Unknown"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    client = web_app.test_client()
    _login(client, "admin", "AdminTestPassword!99")

    dq_html = client.get("/data-quality").get_data(as_text=True)
    token = _extract_csrf(dq_html)
    imp_resp = client.post(
        "/data-quality/run-import",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    assert imp_resp.status_code == 200
    body = imp_resp.get_data(as_text=True)
    assert "1 accepted" in body
    assert "MALFORMED_ISBN" in body

    # Change own password in /settings
    set_html = client.get("/settings").get_data(as_text=True)
    token_pw = _extract_csrf(set_html)
    pw_resp = client.post(
        "/settings",
        data={
            "csrf_token": token_pw,
            "current_password": "AdminTestPassword!99",
            "new_password": "NewAdminPassword!2026",
            "confirm_password": "NewAdminPassword!2026",
        },
        follow_redirects=True,
    )
    assert pw_resp.status_code == 200
    assert "Your password has been updated" in pw_resp.get_data(as_text=True)

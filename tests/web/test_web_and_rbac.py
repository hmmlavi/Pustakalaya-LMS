"""Web route, session authentication, CSRF protection, RBAC, and CSV export tests."""

from __future__ import annotations

from datetime import date, datetime, timezone
import re

from app.services.reporting_service import sanitize_csv_value


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token"\s+type="hidden"\s+value="([^"]+)"', html)
    if not match:
        match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert match is not None, "Expected CSRF hidden token in HTML form"
    return match.group(1)


def _login(client, username: str, password: str):
    login_get = client.get("/auth/login")
    assert login_get.status_code == 200
    token = _extract_csrf(login_get.get_data(as_text=True))
    return client.post(
        "/auth/login",
        data={"username": username, "password": password, "csrf_token": token},
        follow_redirects=True,
    )


def test_unauthenticated_routes_redirect_to_login(web_app):
    client = web_app.test_client()
    for path in ["/", "/books", "/copies", "/members", "/circulation", "/reports"]:
        resp = client.get(path)
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]


def test_csrf_protection_blocks_forged_post_requests(web_app):
    client = web_app.test_client()
    forged = client.post(
        "/auth/login",
        data={"username": "admin", "password": "AdminTestPassword!99"},
    )
    assert forged.status_code == 400
    assert "CSRF" in forged.get_data(as_text=True)

    _login(client, "admin", "AdminTestPassword!99")
    forged_book = client.post("/books/new", data={"title": "Forged"})
    assert forged_book.status_code == 400


def test_rbac_enforcement_for_staff_readonly(web_app):
    client = web_app.test_client()
    resp = _login(client, "readonly", "ReadonlyPassword!99")
    assert resp.status_code == 200
    assert "Network Operations Dashboard" in resp.get_data(as_text=True)

    # Read-only staff can browse all non-admin GET views
    for get_path in [
        "/",
        "/books",
        "/books/authors",
        "/copies",
        "/members",
        "/members/tiers",
        "/circulation",
        "/reservations",
        "/fines",
        "/fines/payments",
        "/reports?type=overdue",
        "/reports?type=inventory",
        "/data-quality",
        "/settings",
    ]:
        r = client.get(get_path)
        assert r.status_code == 200, f"Expected 200 for {get_path}, got {r.status_code}"

    # Read-only staff blocked from /admin and mutating endpoints -> 403
    assert client.get("/admin").status_code == 403
    assert client.get("/books/new").status_code == 403
    assert client.get("/members/new").status_code == 403

    settings_page = client.get("/settings")
    token = _extract_csrf(settings_page.get_data(as_text=True))
    assert (
        client.post(
            "/circulation/issue",
            data={
                "member_identifier": "MEM-000001",
                "copy_identifier": "X",
                "csrf_token": token,
            },
        ).status_code
        == 403
    )


def test_rbac_enforcement_for_librarian_vs_admin(web_app):
    client = web_app.test_client()
    _login(client, "librarian", "LibrarianPassword!99")

    assert client.get("/members/new").status_code == 200
    assert client.get("/admin").status_code == 403
    assert client.get("/books/new").status_code == 403


def test_end_to_end_web_workflow_and_csv_export(web_app, db_env):
    client = web_app.test_client()
    _login(client, "admin", "AdminTestPassword!99")

    # 1. Create catalog book via web form
    new_book_get = client.get("/books/new")
    assert new_book_get.status_code == 200
    token = _extract_csrf(new_book_get.get_data(as_text=True))
    create_resp = client.post(
        "/books/new",
        data={
            "csrf_token": token,
            "title": "Umrao Jaan Ada",
            "subtitle": "Classic Urdu Novel of Lucknow",
            "isbn": "978-0-306-40615-7",
            "author_names": "Mirza Hadi Ruswa",
            "publisher": "Orient BlackSwan",
            "publication_year": "1905",
            "category": "Urdu Literature & Shayari",
            "language": "Urdu",
            "edition": "Critical Edition",
            "description": "Historical Lucknow literary classic.",
        },
        follow_redirects=True,
    )
    assert create_resp.status_code == 200
    assert "Umrao Jaan Ada" in create_resp.get_data(as_text=True)

    # 2. Register physical copy via web form
    b1_id = db_env["branches"][0].id
    copies_get = client.get("/copies")
    token = _extract_csrf(copies_get.get_data(as_text=True))
    add_copy_resp = client.post(
        "/copies/new",
        data={
            "csrf_token": token,
            "book_id": "1",
            "owning_branch_id": str(b1_id),
            "accession_number": "LKO-HZR-WEB-001",
            "acquisition_date": "2026-01-10",
            "status": "AVAILABLE",
            "condition_notes": "New hardcover",
        },
        follow_redirects=True,
    )
    assert add_copy_resp.status_code == 200
    assert "LKO-HZR-WEB-001" in add_copy_resp.get_data(as_text=True)

    # 3. Register member via web form
    reg_get = client.get("/members/new")
    token = _extract_csrf(reg_get.get_data(as_text=True))
    reg_resp = client.post(
        "/members/new",
        data={
            "csrf_token": token,
            "full_name": "Farhan Siddiqui",
            "email": "farhan@example.org",
            "phone": "+91-9839011223",
            "address": "Aminabad, Lucknow",
            "tier_code": "STANDARD",
            "home_branch_id": str(b1_id),
            "expiry_date": "2027-10-24",
            "notes": "",
        },
        follow_redirects=True,
    )
    assert reg_resp.status_code == 200
    assert "MEM-000001" in reg_resp.get_data(as_text=True)

    # 4. Issue copy via web form
    circ_get = client.get("/circulation")
    token = _extract_csrf(circ_get.get_data(as_text=True))
    issue_resp = client.post(
        "/circulation/issue",
        data={
            "csrf_token": token,
            "member_identifier": "MEM-000001",
            "copy_identifier": "LKO-HZR-WEB-001",
            "issuing_branch_id": str(b1_id),
        },
        follow_redirects=True,
    )
    assert issue_resp.status_code == 200
    assert "LKO-HZR-WEB-001" in issue_resp.get_data(as_text=True)

    # 5. Make an overdue loan via service and verify Reports + CSV download
    c2 = db_env["books"].add_copy(
        book_id=1,
        owning_branch_id=b1_id,
        accession_number="LKO-HZR-WEB-002",
        acquisition_date=date(2026, 1, 10),
        actor_user_id=db_env["admin_user"].id,
    )
    db_env["circulation"].issue_copy(
        member_identifier="MEM-000001",
        copy_identifier=c2.accession_number,
        issuing_branch_id=b1_id,
        actor_user_id=db_env["admin_user"].id,
        checkout_at=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc),
    )

    overdue_html = client.get("/reports?type=overdue").get_data(as_text=True)
    assert "LKO-HZR-WEB-002" in overdue_html

    csv_resp = client.get("/reports/overdue.csv?mode=active")
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["Content-Type"]
    assert "LKO-HZR-WEB-002" in csv_resp.get_data(as_text=True)

    # 6. Admin sections render cleanly
    for admin_path in [
        "/admin",
        "/admin/users",
        "/admin/branches",
        "/admin/tiers",
        "/admin/holidays",
        "/admin/audit",
        "/admin/corrections",
    ]:
        assert client.get(admin_path).status_code == 200


def test_csv_formula_injection_neutralization():
    assert sanitize_csv_value("=CMD|' /C calc'!A0") == "'=CMD|' /C calc'!A0"
    assert sanitize_csv_value("+SUM(A1:A2)") == "'+SUM(A1:A2)"
    assert sanitize_csv_value("-10+20") == "'-10+20"
    assert sanitize_csv_value("@恶意") == "'@恶意"
    assert sanitize_csv_value("Normal Title") == "Normal Title"


def test_first_run_admin_setup_locks_after_initial_user(tmp_path):
    from app import create_app
    from app.config import AppConfig
    from app.infrastructure.database import apply_migrations

    empty_db = tmp_path / "first_run.db"
    db_url = f"sqlite:///{empty_db.as_posix()}"
    apply_migrations(db_url)

    app = create_app(
        AppConfig(
            database_url=db_url,
            secret_key="test-first-run-secret-key-0123456789abcdef",
            testing=True,
            csrf_enabled=True,
        )
    )
    client = app.test_client()

    # 1. Login page advertises First-Run Setup when zero users exist
    login_html = client.get("/auth/login").get_data(as_text=True)
    assert "Create Initial Administrator" in login_html

    # 2. First-run setup creates initial ADMIN and signs in
    setup_get = client.get("/auth/setup")
    assert setup_get.status_code == 200
    token = _extract_csrf(setup_get.get_data(as_text=True))
    setup_post = client.post(
        "/auth/setup",
        data={
            "csrf_token": token,
            "username": "firstadmin",
            "full_name": "First Run Chief Librarian",
            "email": "first@pustakalaya.local",
            "password": "StrongSetupPass#2026",
            "confirm_password": "StrongSetupPass#2026",
        },
        follow_redirects=True,
    )
    assert setup_post.status_code == 200
    assert "firstadmin" in setup_post.get_data(as_text=True)

    # 3. Once any user exists, /auth/setup redirects GET and blocks POST with 403
    logout_page = client.get("/")
    logout_token = _extract_csrf(logout_page.get_data(as_text=True))
    client.post("/auth/logout", data={"csrf_token": logout_token})

    second_get = client.get("/auth/setup")
    assert second_get.status_code == 302
    assert "/auth/login" in second_get.headers["Location"]

    login_token = _extract_csrf(client.get("/auth/login").get_data(as_text=True))
    second_post = client.post(
        "/auth/setup",
        data={
            "csrf_token": login_token,
            "username": "rogueadmin",
            "full_name": "Rogue User",
            "password": "StrongSetupPass#2026",
            "confirm_password": "StrongSetupPass#2026",
        },
    )
    assert second_post.status_code == 403


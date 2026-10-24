# Automated Testing & Verification Guide

Pustakalaya includes **57 automated pytest tests** covering pure domain logic, database integration, SQLite concurrency, atomic transaction rollback, historical data import quarantine/idempotency, Alembic migrations, DOM accessibility (`id` uniqueness & `<label for>` associations), security headers/open-redirect guards, visual-theme behavior, and all 12 web workflows with RBAC enforcement.

## 1. Isolation Strategy

- Every test uses pytest's `tmp_path` fixture (`tests/conftest.py`) to create an isolated temporary SQLite database file (`test_pustakalaya.db`).
- Tests **never** touch or mutate `instance/pustakalaya.db`.
- Integration and concurrency tests execute against real SQLite engines with `PRAGMA foreign_keys = ON`, `WAL` mode, partial unique indexes, and `BEGIN IMMEDIATE` transactions rather than mocking repositories.

## 2. Test Suite Breakdown (`tests/`)

| Test Module | Count | What Is Verified |
| :--- | :---: | :--- |
| `tests/unit/test_domain_rules.py` | 13 | ISBN-10 (`X` check digit) & ISBN-13 checksums, optional blank ISBNs, `Decimal` quantization & `float` rejection, email/phone normalization, `CopyStateMachine`, `ReservationStateMachine`, `HolidayCalendar`, `HolidayAwareFinePolicy` (grace days, holidays, cap), `TierBorrowingPolicy` (loan limits, fine threshold, overdue block, expiry, renewal restrictions) |
| `tests/integration/test_circulation_and_reservations.py` | 5 | Duplicate ISBN & non-ISBN duplicate guards, multi-author attribution, tier loan limits, suspended member checkout block, full FIFO reservation queue -> automatic return allocation -> reserved pickup guard -> deadline expiry reallocation -> pickup fulfillment, damaged return & lost-copy workflows |
| `tests/integration/test_fines_concurrency_and_rollback.py` | 4 | Overdue fine calculation, partial & full payments, overpayment rejection, `idempotency_key` deduplication, fine-threshold checkout block & unblock, admin waiver, **2-thread SQLite concurrent double-checkout race (`ThreadPoolExecutor` + `Barrier`)**, **simulated mid-transaction crash (`_before_commit_hook`) proving atomic rollback of loans/fines/copies/`AuditEvent`**, and SQLite `PRAGMA foreign_keys = ON` enforcement |
| `tests/integration/test_historical_import.py` | 1 | End-to-end import of 13 curated historical records spanning valid loans/fines/payments/reservations plus 10 distinct corruption codes, followed by a second run proving 100% idempotency |
| `tests/integration/test_admin_auth_and_reports.py` | 6 | Werkzeug password hashing & verification, inactive account lockouts, last-admin demotion protection, dynamic tier policy updates affecting future loans without corrupting existing loans, cancelling `READY_FOR_PICKUP` hold releasing copy, live dashboard & branch overdue report accuracy, holiday & due-date correction |
| `tests/integration/test_migrations_and_more.py` | 8 | Programmatic Alembic `upgrade head` idempotency across all 16 tables, cross-branch checkout mismatch guard, double-return guard, renewal count progression & cap, damaged hold-shelf copy returning reservation to `WAITING`, web `POST` flows for return/fine-pay/waive, reservation create/cancel, `0`-grace tier update, Data Quality web import, and `/settings` password change |
| `tests/web/test_web_and_rbac.py` | 7 | Unauthenticated `/auth/login` redirects, first-run `/auth/setup` lock-down after initial admin creation, `CSRFProtect` HTTP `400` rejection on forged `POST`s, `STAFF_READONLY` vs `LIBRARIAN` vs `ADMIN` HTTP `403` enforcement, end-to-end web catalog/copy/member/issue/overdue-CSV workflow, and OWASP CSV formula-injection neutralization |
| `tests/web/test_audit_regressions_and_workflows.py` | 7 | FIFO hold allocation on newly acquired/repaired `AVAILABLE` copies, `update_book` non-ISBN duplicate guard, admin correction IST local-date & active-loan guards, production cookie & HTTP security/open-redirect hardening, full HTML DOM unique-`id` & `<label for>` accessibility audit across 29 rendered views, all 12 web workflows (persistence on success + non-modification on failure), and isolated repeated-seed/repeated-import `verify_data` check |
| `tests/web/test_living_library_experience.py` | 6 | Offline asset loading, theme and ambience controls, responsive navigation, and library dashboard presentation regressions |

## 3. Running Tests & Verification

### Linux / macOS
```bash
.venv/bin/pytest -v
.venv/bin/python scripts/verify_data.py
```

### Windows PowerShell
```powershell
.\.venv\Scripts\pytest -v
.\.venv\Scripts\python scripts\verify_data.py
```

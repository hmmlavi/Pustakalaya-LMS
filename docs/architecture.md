# Pustakalaya — System Architecture

**Pustakalaya** (*पुस्तकालय*, *"Every Book, One Place."*) is a modular, four-layer Library Management System built for a 3-branch public library network in Lucknow, Uttar Pradesh, India (`Hazratganj Central Library`, `Gomti Nagar Civic Branch`, and `Aliganj Neighbourhood Branch`).

## 1. Four-Layer Architecture & Dependency Direction

Dependencies flow strictly inward toward the domain model:

```text
Presentation Layer (app/web/)
       │  HTTP, Flask blueprints, Flask-WTF CSRF forms, Jinja2 templates, RBAC decorators
       ▼
Application / Service Layer (app/services/)
       │  Use-case orchestration, UnitOfWork transaction boundaries, audit recording
       ▼
Persistence / Repository Layer (app/repositories/ + app/models/ + app/infrastructure/)
       │  SQLAlchemy 2.0 queries, pagination, conditional SQL updates, Alembic migrations
       ▼
Domain Layer (app/domain/)
          Pure Python business policies, value objects (ISBN, Decimal money), enums,
          state machines, Strategy & Factory interfaces — zero Flask or SQLite imports
```

### Layer Responsibilities

1. **Layer 1 — Domain (`app/domain/`)**
   - `enums.py`: Strongly typed domain enumerations (`CopyStatus`, `LoanStatus`, `ReservationStatus`, `FineStatus`, `MembershipStatus`, `TierCode`, `UserRole`, `PaymentMethod`, `AuditEventType`, `ImportDisposition`).
   - `value_objects.py`: `ISBN` (ISBN-10 including `X` check digit and ISBN-13 checksum validation & normalization), `quantize_money` (exact 2-decimal `Decimal` arithmetic that explicitly rejects binary `float` inputs), email/phone normalization, and UTC vs `Asia/Kolkata` local-date utilities.
   - `state_machines.py`: `CopyStateMachine` and `ReservationStateMachine` enforcing every legal and illegal transition.
   - `policies.py`: `BorrowingPolicyStrategy` (`TierBorrowingPolicy`), `FineCalculationStrategy` (`HolidayAwareFinePolicy`), `HolidayCalendar`, and `PolicyFactory`.
   - `exceptions.py`: Expressive domain exception hierarchy (`DomainError` subclasses) translated cleanly into user-facing feedback at the web boundary.

2. **Layer 2 — Persistence & Repositories (`app/models/`, `app/repositories/`, `app/infrastructure/`)**
   - `app/models/base.py`: Custom SQLAlchemy `TypeDecorator` implementations:
     - `MoneyPaise`: Stores INR `Decimal('12.50')` as exact integer paise (`1250`) in SQLite `INTEGER` columns, eliminating floating-point drift in SQLite aggregations and `CHECK` constraints.
     - `UTCDateTime`: Enforces timezone-aware UTC `datetime` persistence across all event columns.
   - `app/models/orm.py`: 16 normalized relational tables with foreign keys, `CHECK` constraints, and partial unique indexes.
   - `app/repositories/`: 9 repository modules (`branch_repository`, `book_repository`, `copy_repository`, `member_repository`, `loan_repository`, `reservation_repository`, `fine_repository`, `user_repository`, `audit_repository`) + `base.py` pagination helper, providing 12 specialized repository classes (`BranchRepository`, `AuthorRepository`, `BookRepository`, `CopyRepository`, `TierRepository`, `MemberRepository`, `UserRepository`, `LoanRepository`, `ReservationRepository`, `FineRepository`, `HolidayRepository`, `AuditRepository`, and `ImportBatchRepository`).
   - `app/infrastructure/database.py` & `transactions.py`: Configures SQLite connections (`PRAGMA foreign_keys = ON`, `PRAGMA journal_mode = WAL`, `PRAGMA busy_timeout = 15000`, `PRAGMA synchronous = NORMAL`) and provides `UnitOfWork` / `write_transaction` (`BEGIN IMMEDIATE`) and `read_session`.

3. **Layer 3 — Application Services (`app/services/`)**
   - Nine cohesive services: `AuthenticationService`, `AdministrationService`, `BookService`, `MemberService`, `CirculationService`, `ReservationService`, `FineService`, `ReportingService`, and `DataImportService`.
   - Every mutating workflow executes inside a single `write_transaction`, ensuring operational state changes and `AuditEvent` entries commit or roll back together.

4. **Layer 4 — Presentation (`app/web/`)**
   - `routes/`: 12 Flask blueprints (`auth`, `dashboard`, `books`, `copies`, `members`, `circulation`, `reservations`, `fines`, `reports`, `data_quality`, `admin`, `settings`) covering all 11 navigation sections.
   - `forms.py`: Server-validated Flask-WTF forms with global `CSRFProtect`.
   - `auth_utils.py`: Session loader (`load_logged_in_user`), `@login_required`, and `@roles_required(*roles)` server-side authorization decorators.
   - `templates/` & `static/`: Local CSS (`main.css`) and progressive-enhancement JS (`app.js`) with zero external CDNs, icon packs, or webfont requests.

## 2. Transaction & Concurrency Architecture on SQLite

Preventing double-checkout or inconsistent queue allocation under concurrent requests on SQLite is achieved through four cooperating mechanisms:

1. **`BEGIN IMMEDIATE` Write Transactions (`app/infrastructure/transactions.py`)**: Every `write_transaction` acquires SQLite's reserved write lock up front before reading copy/member state, serializing concurrent writers cleanly within the 15,000 ms `busy_timeout`.
2. **Conditional Atomic Row Transitions (`CopyRepository.conditional_transition_status`, `LoanRepository.conditional_apply_renewal`)**: Status transitions execute `UPDATE book_copies SET status = 'ON_LOAN' WHERE id = :id AND status IN ('AVAILABLE', 'RESERVED')` and verify `rowcount == 1`.
3. **Database-Level Partial Unique Indexes (`app/models/orm.py`)**:
   - `uq_loans_one_active_per_copy`: `UNIQUE(copy_id) WHERE status = 'ACTIVE'`
   - `uq_reservations_one_active_per_member_book`: `UNIQUE(member_id, book_id) WHERE status IN ('WAITING', 'READY_FOR_PICKUP')`
   - `uq_reservations_one_ready_per_copy`: `UNIQUE(allocated_copy_id) WHERE status = 'READY_FOR_PICKUP' AND allocated_copy_id IS NOT NULL`
4. **Atomic Rollback**: Unhandled exceptions anywhere prior to commit trigger `session.rollback()`, reverting loan, copy, fine, reservation, and `AuditEvent` writes together.

## 3. CLI & Operational Entrypoints

- `run.py` — Starts the Flask web application (`http://127.0.0.1:5000` / `0.0.0.0:5000`).
- `scripts/initialize_db.py` — Applies Alembic migrations (`upgrade head`) and optionally bootstraps the initial `ADMIN` user (`--prompt-admin-password` or env vars).
- `scripts/seed_data.py` — Deterministically seeds the 3 Lucknow branches, 3 tiers, 16 holidays, 140 authors, 2,000 books, 4,200 physical copies, 500 members, and exports the 1,417-record historical fixture (`instance/historical_source_records.json`).
- `scripts/import_history.py` — Runs the idempotent historical import pipeline and prints a structured validation report.
- `scripts/process_reservations.py` — Idempotently expires unclaimed `READY_FOR_PICKUP` holds past their deadline and reallocates copies to the next eligible FIFO waiter.
- `scripts/verify_data.py` — Verifies all dataset counts and relational invariants (`exit 0` on pass, `exit 1` on any violation).

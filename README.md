# Pustakalaya (पुस्तकालय)

> **Every Book, One Place.**  
> A modular, offline-ready Library Management System built for the **Lucknow Public Library Network** (Hazratganj Central Library, Gomti Nagar Civic Branch, and Aliganj Neighbourhood Branch).

---

## Download the Windows release

The latest stable Windows release is available on the [Pustakalaya-LMS v1.0.0 release page](https://github.com/hmmlavi/Pustakalaya-LMS/releases/tag/v1.0.0):

- [Pustakalaya.exe](https://github.com/hmmlavi/Pustakalaya-LMS/releases/download/v1.0.0/Pustakalaya.exe) — standalone Windows executable.
- [Pustakalaya-windows-portable.zip](https://github.com/hmmlavi/Pustakalaya-LMS/releases/download/v1.0.0/Pustakalaya-windows-portable.zip) — portable package.
- [SHA256SUMS.txt](https://github.com/hmmlavi/Pustakalaya-LMS/releases/download/v1.0.0/SHA256SUMS.txt) — download checksums.

See [Quick Start — Windows](#quick-start--windows-powershell) below for first-run and data-storage instructions.

## Key Capabilities

- **4-Layer Architecture & Rich Domain Model**: Clean separation across Domain (`app/domain`), Persistence/Repositories (`app/models`, `app/repositories`, `app/infrastructure`), Application Services (`app/services`), and Web Presentation (`app/web`).
- **Catalog & Branch Inventory**: ISBN-10/ISBN-13 checksum validation, optional missing-ISBN support for heritage imprints, many-to-many author attribution, unique copy accession numbers, and validated copy-state transitions (`AVAILABLE`, `ON_LOAN`, `RESERVED`, `LOST`, `DAMAGED`, `WITHDRAWN`).
- **Configurable Membership Tiers (Strategy + Factory)**: `BASIC`, `STANDARD`, and `PREMIUM` policies governing max simultaneous loans, loan/renewal durations, reservation limits, grace days, daily overdue rates, per-loan fine caps, and unpaid-fine blocking thresholds.
- **Atomic Circulation & SQLite Concurrency Safety**: Issue, return, damaged/lost workflows, and policy-checked renewals protected by SQLite `WAL` mode, `BEGIN IMMEDIATE` write transactions, partial unique indexes, and conditional row updates—preventing concurrent double-checkout and rolling back all partial writes (including audit events) on failure.
- **Deterministic FIFO Reservations**: Queue ordering by `(created_at, id)`, automatic hold allocation on return, pickup-deadline tracking, and idempotent expiration reallocation (`scripts/process_reservations.py`).
- **Exact INR Fine Ledger**: `Decimal` arithmetic backed by integer paise storage (`MoneyPaise`), holiday- and grace-period-aware overdue calculations, partial and full payments, idempotency keys, and audited administrative waivers.
- **Historical Data Quality & Quarantine Pipeline**: Imports 1 year of realistic synthetic history while rejecting or quarantining malformed ISBNs, ghost references, missing/inverted dates, overlapping loans, and unexplained fine discrepancies into an inspectable staging ledger.
- **Premium Skeuomorphic Botanical & Moonlit Forest UI**: Tactile 5-level skeuomorphic depth system, physical hardcover bookplate placeholders, responsive button/input micro-interactions, and dual cool-toned themes—**Peaceful Botanical Light** (`#F2F6F1` / `#FFFFFF` / `#668875` / `#91B5B5`) and **Moonlit Forest Dark** (`#101A19` / `#172422` / `#8AB6A0` / `#A7C5D5` with cool celadon lanterns and pale bioluminescent fireflies)—paired with a zero-FOUC Horizon Toggle, independent **Library Ambience** controls (`Auto`, `Minimal`, `Off`), `prefers-reduced-motion` support, and 100% offline operation.
- **Local Security & RBAC**: Werkzeug password hashing, Flask-WTF CSRF protection on all state-changing endpoints, session expiration, server-side role enforcement (`ADMIN`, `LIBRARIAN`, `STAFF_READONLY`), CSV formula-injection protection, and zero external CDN/API dependencies.

---

## Quick Start — Windows (PowerShell)

```powershell
# 1. Create and activate an isolated virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install runtime and test dependencies
pip install -r requirements-dev.txt

# 3. Create your local environment configuration
Copy-Item .env.example .env
# Edit .env to set a strong random SECRET_KEY if desired (ephemeral key is generated in dev if omitted)

# 4. Run Alembic database migrations and create your initial Administrator account
python scripts\initialize_db.py --admin-username admin --prompt-admin-password
# (Alternatively, run `alembic upgrade head`, start `python run.py`, and visit `/auth/setup` once;
#  first-run setup locks automatically as soon as the first staff account exists.)

# 5. Seed the deterministic Lucknow 3-branch dataset (2,000 books, 4,200 copies, 500 members)
python scripts\seed_data.py

# 6. Import the 1-year historical dataset (validates, accepts valid rows, quarantines malformed rows)
python scripts\import_history.py

# 7. Process any expired reservation pickup holds & verify all dataset counts and relational invariants
python scripts\process_reservations.py
python scripts\verify_data.py

# 8. Run the automated test suite (57 tests on isolated temporary databases)
pytest -v

# 9. Launch the web application (http://127.0.0.1:5000)
python run.py
```

---

## Quick Start — Linux / macOS

```bash
# 1. Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements-dev.txt

# 3. Copy environment template
cp .env.example .env

# 4. Apply Alembic migrations & bootstrap initial administrator
python scripts/initialize_db.py --admin-username admin --prompt-admin-password

# 5. Seed deterministic 3-branch dataset & generate historical fixture
python scripts/seed_data.py

# 6. Run idempotent historical import & verify database health
python scripts/import_history.py
python scripts/verify_data.py

# 7. Run automated test suite
pytest -v

# 8. Start Pustakalaya web server
python run.py
```

---

## Operational CLI Commands

| Command | Purpose |
| :--- | :--- |
| `alembic upgrade head` | Apply versioned schema migrations directly via Alembic |
| `python scripts/initialize_db.py --prompt-admin-password` | Apply migrations and interactively bootstrap an `ADMIN` account |
| `python scripts/seed_data.py` | Idempotently seed branches, tiers, holidays, 2,000 books, 4,200 copies, 500 members, and export `instance/historical_source_records.json` |
| `python scripts/import_history.py` | Validate and import historical records idempotently with structured quarantine reporting |
| `python scripts/process_reservations.py` | Expire unclaimed `READY_FOR_PICKUP` holds past their deadline and reallocate copies to next FIFO waiters |
| `python scripts/verify_data.py` | Assert all dataset counts, FK integrity, copy/loan/reservation/fine invariants (`exit 0` on pass) |

---

## Project Structure & Documentation

```text
Pustakalaya-LMS/
├── app/
│   ├── config.py                # Environment configuration & cross-platform SQLite path resolution
│   ├── domain/                  # Pure domain entities, enums, ISBN/Money value objects, state machines, policies
│   ├── infrastructure/          # SQLite WAL/FK engine setup, BEGIN IMMEDIATE UnitOfWork, logging, seed generator
│   ├── models/                  # SQLAlchemy 2.0 ORM mappings (16 tables, MoneyPaise, UTCDateTime, partial indexes)
│   ├── repositories/            # 9 repository modules encapsulating persistence queries & pagination
│   ├── services/                # 9 application services orchestrating transactional use cases & audit logging
│   └── web/                     # 12 Flask blueprints (11 UI sections), CSRF forms, RBAC decorators, templates, CSS/JS
├── migrations/                  # Versioned Alembic schema migrations
├── scripts/                     # CLI setup, seed, historical import, reservation processor, and verification tools
├── tests/                       # 57 pytest unit, integration, concurrency, rollback, import, DOM, theme, and web/RBAC tests
├── docs/
│   ├── architecture.md          # 4-layer architecture & SQLite transaction/concurrency model
│   ├── class-diagram.mmd        # Mermaid OOP class diagram (entities, policies, repositories, services)
│   ├── er-diagram.mmd           # Mermaid relational ER diagram (all 16 tables & constraints)
│   ├── design-decisions.md      # Repository, Strategy, Factory, Unit of Work, & MoneyPaise rationale
│   ├── business-rules.md        # Tier rules, state transitions, FIFO holds, holiday-aware fines, RBAC matrix
│   ├── data-quality.md          # Synthetic dataset metrics & historical quarantine issue-code reference
│   └── testing.md               # Test suite coverage & execution guide
├── instance/                    # Git-ignored runtime SQLite DB, logs, and generated historical fixture
└── run.py                       # Local WSGI/Flask server entrypoint
```

## Continuous integration

The [`CI` workflow](.github/workflows/ci.yml) runs on pushes to `main` and on pull requests targeting `main`.
It installs the documented development dependencies, checks dependency consistency, verifies a fresh Alembic
database migration, and runs the complete pytest suite. Windows packaging and release publication remain isolated
to the tagged-release workflow.

---

## Known Limitations & Local Deployment Notes

## Windows release

Tagged releases are built by [`.github/workflows/release-windows.yml`](.github/workflows/release-windows.yml)
on a Windows runner after the test suite passes. Each release contains a portable ZIP,
the standalone launcher executable, and `SHA256SUMS.txt`.

To build the same artifacts locally on Windows:

```powershell
python -m pip install -r requirements-dev.txt pyinstaller==6.16.0
.\scripts\build_windows.ps1
```

The launcher starts the local server on `127.0.0.1`, waits until it responds, and then
opens the browser. Installed data is stored in `%LOCALAPPDATA%\Pustakalaya` by default;
set `PUSTAKALAYA_DATA_DIR` to choose another writable location. The first run still
requires administrator setup, either in the browser at `/auth/setup` or with
`scripts\initialize_db.py`. The executable is a portable deployment, not a system-wide
installer, so it does not require administrator privileges.

- **Single-Host SQLite Scope**: SQLite `WAL` + `BEGIN IMMEDIATE` (`busy_timeout = 15000 ms`) safely serializes concurrent branch desk requests on a single host, but is not intended for multi-node shared-network-filesystem deployments without migrating `DATABASE_URL` to a client-server RDBMS.
- **Local HTTP vs. Production HTTPS**: In `FLASK_ENV=development` (`http://127.0.0.1:5000`), `SESSION_COOKIE_SECURE` defaults to `False` so browser sessions work over local loopback HTTP. Setting `FLASK_ENV=production` requires a persistent `SECRET_KEY` and enables `SESSION_COOKIE_SECURE=True` (serve behind TLS/HTTPS).
- **Scheduled Hold Expiration**: Unclaimed `READY_FOR_PICKUP` holds are expired via **Reservations → Process Expired Pickup Holds** or `python scripts/process_reservations.py` (schedule via Windows Task Scheduler or cron for unattended background sweeps).
- **No Built-In Login Rate Limiter**: Authentication uses salted `scrypt`/`pbkdf2` Werkzeug password hashes; internet-facing deployments should enforce upstream reverse-proxy rate limiting and TLS.

# Architectural & Design Decisions

This document explains the object-oriented design patterns and persistence trade-offs implemented in Pustakalaya.

## 1. Applied Design Patterns

### A. Repository Pattern (`app/repositories/`)
- **Problem**: Embedding SQLAlchemy `select()`/`update()` statements directly inside Flask routes or business workflows couples business rules to ORM query syntax and causes subtle join-multiplication bugs in reports.
- **Implementation**: Nine repository modules defining thirteen repository classes (`BranchRepository`, `HolidayRepository`, `BookRepository`, `AuthorRepository`, `CopyRepository`, `MemberRepository`, `TierRepository`, `LoanRepository`, `ReservationRepository`, `FineRepository`, `UserRepository`, `AuditRepository`, `ImportBatchRepository`) encapsulate all persistence queries, eager-loading options, FIFO queue ordering (`ORDER BY created_at ASC, id ASC`), and conditional atomic updates.
- **Benefit**: Services read clearly in domain terms (`loan_repo.get_active_loan_for_copy(copy.id)`), pagination is standardized via `paginate_statement`, and reporting queries avoid row multiplication.

### B. Strategy Pattern (`app/domain/policies.py`)
- **Problem**: Borrowing rules (simultaneous loan limits, loan duration, max renewals, reservation limits, blocking fine thresholds) and overdue fine rules (grace days, closed-holiday exclusions, per-loan caps) vary across `BASIC`, `STANDARD`, and `PREMIUM` membership tiers and may evolve over time.
- **Implementation**:
  - `BorrowingPolicyStrategy` (abstract interface) implemented by `TierBorrowingPolicy`.
  - `FineCalculationStrategy` (abstract interface) implemented by `HolidayAwareFinePolicy`.
  - Both strategies operate purely on immutable `TierPolicyConfig` and `HolidayCalendar` value objects in `app/domain/policies.py`, with zero database or web dependencies.
- **Benefit**: All borrowing, renewal, due-date, and fine edge cases are unit-tested purely in memory in milliseconds while remaining runtime-configurable by administrators via the database.

### C. Factory Pattern (`PolicyFactory` in `app/domain/policies.py`)
- **Problem**: Services need to instantiate the right `BorrowingPolicyStrategy` and `FineCalculationStrategy` from a `MembershipTier` entity without duplicating parameter extraction or `Decimal` quantization across `MemberService`, `CirculationService`, `ReservationService`, `FineService`, and `DataImportService`.
- **Implementation**: `PolicyFactory.create_borrowing_policy(tier)` and `PolicyFactory.create_fine_policy(tier, calendar)` convert ORM `MembershipTier` entities into immutable `TierPolicyConfig` snapshots and return ready-to-use strategy objects.

### D. Unit of Work Pattern (`UnitOfWork` / `write_transaction` in `app/infrastructure/transactions.py`)
- **Problem**: A single circulation event (such as returning an overdue book that has a waiting queue) mutates `Loan`, `Fine`, `BookCopy`, `Reservation`, and `AuditEvent` rows. Committing inside individual repositories would allow partial writes if a later step fails.
- **Implementation**: Repositories never call `session.commit()`. Instead, application services open a `UnitOfWork(session_factory, write=True)` context (`write_transaction`), pass the shared `Session` to all participating repositories, commit atomically at the end of the `with` block, or roll back everything on any exception.

### E. Explicit Domain State Machines (`CopyStateMachine`, `ReservationStateMachine`)
- **Problem**: Ad-hoc string assignments (`copy.status = "AVAILABLE"`) allow illegal transitions such as checking out a `WITHDRAWN` or `DAMAGED` copy or manually marking an `ON_LOAN` copy `AVAILABLE` while its loan remains open.
- **Implementation**: `BookCopy.transition_to(...)` and `Reservation` lifecycle methods (`allocate_copy`, `mark_fulfilled`, `cancel`, `expire`) delegate to `CopyStateMachine` and `ReservationStateMachine`, raising `InvalidStateTransitionError` or `ReservationConflictError` on any invalid transition.

## 2. Key Engineering Trade-Offs

### Storing Money as Integer Paise (`MoneyPaise`) + Python `Decimal`
SQLite lacks a fixed-point `NUMERIC` hardware storage class (its `NUMERIC` affinity can coerce values to IEEE-754 64-bit floats). By mapping monetary columns through `MoneyPaise` (`INTEGER` paise in SQLite, `Decimal('0.01')` in Python) and rejecting `float` inputs in `quantize_money()`, SQLite `SUM()` and `CHECK (paid_amount + waived_amount <= assessed_amount)` constraints are 100% exact to the paisa.

### Timezone-Aware UTC Events vs. Local Operational Dates (`Asia/Kolkata`)
Audit events, checkouts, returns, payments, and pickup deadlines are stored in UTC (`UTCDateTime`) for unambiguous ordering across daylight/system changes. Operational calendar rules (due dates, holiday closures, grace days) convert instants into the configured library timezone (`Asia/Kolkata` by default) via `zoneinfo.ZoneInfo`.

### Staging/Quarantine Ledger for Historical Imports
Legacy library datasets routinely contain missing return dates, malformed ISBNs, or unexplained fine overrides. Rather than loosening database constraints or fabricating synthetic return dates, `DataImportService` executes each source row inside a `SAVEPOINT` (`session.begin_nested()`). Valid rows enter operational tables with `source_reference` idempotency keys; invalid or ambiguous rows roll back their savepoint and are recorded verbatim in `import_issues` (`REJECTED` or `QUARANTINED`) for librarian inspection in the **Data Quality** console.

# Pustakalaya — Business Rules Specification

This document records the authoritative operational rules enforced in the Domain and Service layers.

## 1. Catalog & Physical Copies

- **Catalog Book vs. Physical Copy**: `Book` represents an intellectual work/edition; `BookCopy` represents an individual barcoded/accessioned physical volume owned by one `LibraryBranch`.
- **ISBN Validation**:
  - Optional: Older pamphlets, regional Awadhi/Urdu imprints, and periodicals without ISBNs are stored with `isbn = NULL`. Duplicate non-ISBN entries with identical `(title, publisher, publication_year, edition)` are rejected.
  - When provided, hyphens/spaces are stripped and the value must pass either the **ISBN-10 modulo-11 checksum** (including `X` = 10) or the **ISBN-13 (`978`/`979`) alternating 1-3 modulo-10 checksum**.
- **Copy Lifecycle (`CopyStatus`)**:
  - `AVAILABLE`: On shelf at its owning branch and eligible for issue.
  - `ON_LOAN`: Currently lent under exactly one `ACTIVE` loan. Cannot be manually edited outside return or lost-report workflows.
  - `RESERVED`: Held on the pickup shelf for a specific `READY_FOR_PICKUP` reservation. May only be issued to that reserving member before `pickup_deadline_at`.
  - `DAMAGED`: Routed to conservation/repair; blocked from circulation and reservation hold allocation until repaired (`AVAILABLE`) or `WITHDRAWN`.
  - `LOST`: Reported missing; blocked from circulation unless recovered via audited reinstatement.
  - `WITHDRAWN`: Permanently retired from inventory; terminal except via an Administrator's audited correction (`ADMIN_CORRECTION`).

## 2. Membership Tiers & Standing

Default seeded parameters (administrators may update policies at `/admin/tiers` for future transactions without retroactively altering existing loans):

| Policy Parameter | `BASIC` (Basic Citizen) | `STANDARD` (Standard Reader) | `PREMIUM` (Premium Scholar) |
| :--- | :---: | :---: | :---: |
| Simultaneous Active Loans | 2 | 5 | 10 |
| Standard Loan Duration | 14 days | 21 days | 30 days |
| Max Renewals per Loan | 1 | 2 | 3 |
| Renewal Extension | 7 days | 14 days | 15 days |
| Max Active Reservations | 2 | 4 | 8 |
| Overdue Grace Period | 1 operating day | 2 operating days | 3 operating days |
| Daily Overdue Fine Rate | ₹5.00 / day | ₹3.00 / day | ₹2.00 / day |
| Per-Loan Maximum Fine Cap | ₹150.00 | ₹200.00 | ₹250.00 |
| Borrowing Block Threshold | ₹50.00 | ₹100.00 | ₹200.00 |

A member is **eligible to borrow** only when:
1. Their tier has `borrowing_enabled = True`,
2. `member.status == ACTIVE`,
3. `expiry_date` is `None` or `>= today` (`Asia/Kolkata`),
4. They hold **zero** currently overdue active loans,
5. Their total unpaid fine balance across all fines is strictly below `fine_blocking_threshold` (or `== ₹0.00` if threshold is `₹0.00`),
6. Their active loan count is strictly below `max_active_loans`.

## 3. Circulation: Issue, Return, Damaged/Lost & Renewal

- **Atomic Issue (`CirculationService.issue_copy`)**:
  - Validates branch activity and verifies `issuing_branch_id == copy.owning_branch_id`.
  - Validates member eligibility, copy availability (`AVAILABLE`, or `RESERVED` exclusively for this member with an unexpired pickup deadline), and absence of any active loan on the copy.
  - Computes initial `due_date = adjust_to_operating_day(checkout_date + loan_duration_days)`, automatically rolling past any network-wide or branch holiday.
  - Atomically transitions copy to `ON_LOAN`, creates the `Loan`, fulfills any matching reservation, and logs `LOAN_ISSUED`.
- **Atomic Return (`CirculationService.return_copy`)**:
  - Rejects duplicate returns (`LoanAlreadyReturnedError`) or return dates preceding checkout date.
  - Marks the loan `RETURNED`, calculates and records any overdue `Fine`, and:
    - If `returned_damaged=True`, transitions copy to `DAMAGED` (never allocating a damaged copy to a waiting patron).
    - Otherwise invokes `ReservationService.allocate_copy_to_next_waiting_in_session`: allocates the copy (`RESERVED` + `READY_FOR_PICKUP` + holiday-adjusted pickup deadline) to the earliest eligible FIFO waiter, or sets the copy `AVAILABLE` if no eligible waiter exists.
- **Renewal (`CirculationService.renew_loan`)**:
  - Allowed only on `ACTIVE`, non-overdue loans (`today <= due_date`) when the member is in good standing, `renewal_count < tier.max_renewals`, and no other member holds an active (`WAITING` or `READY_FOR_PICKUP`) reservation on the book.

## 4. Reservations & Deterministic FIFO Queues

- **Creation Rules**: Allowed only when at least one circulatable copy (`AVAILABLE`, `ON_LOAN`, `RESERVED`) exists, **zero** copies are currently `AVAILABLE` (in the requested branch scope), and the member neither holds an active loan nor an active reservation for that book.
- **Automatic Hold Allocation**: Whenever a copy becomes `AVAILABLE`—whether via circulation return (`CirculationService.return_copy`), new copy acquisition (`BookService.add_copy`), or conservation repair (`BookService.change_copy_status(..., AVAILABLE)`)—`ReservationService.allocate_copy_to_next_waiting_in_session` automatically allocates it to the earliest eligible `WAITING` member (`RESERVED` + `READY_FOR_PICKUP` + holiday-adjusted pickup deadline).
- **Deterministic FIFO Ordering**: Waiting queues are ordered by `(created_at ASC, id ASC)`.
- **Pickup Expiration (`process_expired_reservations`)**: Idempotently transitions `READY_FOR_PICKUP` holds with `pickup_deadline_at < now_utc` to `EXPIRED` and immediately reallocates the held copy to the next eligible FIFO waiter (or `AVAILABLE`).

## 5. Holiday-Aware Overdue Fine Formula & Payments

Given `due_date` and `effective_return_date` in `Asia/Kolkata`:
1. If `effective_return_date <= due_date` $\rightarrow$ `₹0.00`.
2. Count `chargeable_days` in `(due_date, effective_return_date]` excluding dates marked closed in `library_holidays` (network-wide or issuing branch).
3. If `chargeable_days <= tier.grace_period_days` $\rightarrow$ `₹0.00`.
4. Otherwise `eligible_overdue_days = chargeable_days - tier.grace_period_days`.
5. `assessed_amount = min(quantize_money(eligible_overdue_days * daily_fine_rate), max_fine_per_loan)`.
- **Settlements**: Partial payments set status to `PARTIALLY_PAID`; full settlement sets `PAID`. Overpayments and non-positive amounts are rejected. `assessed_amount` is never overwritten when payments or waivers are applied. Duplicate submissions carrying the same `idempotency_key` and amount return the original `FinePayment` receipt without double-charging.

## 6. Role-Based Access Control (RBAC)

All permissions are enforced server-side (`@login_required`, `@roles_required`):
- **`STAFF_READONLY`**: Read-only access to all operational views and CSV exports, plus changing own password in `/settings`. All operational `POST` actions and `/admin/*` return HTTP `403`.
- **`LIBRARIAN`**: All `STAFF_READONLY` capabilities plus member registration/updates/status changes, circulation issue/return/renew/lost reports, reservation creation/cancellation/expiration processing, and recording fine payments.
- **`ADMIN`**: Full access including catalog/author/copy creation and edits, fine waivers, historical data imports, staff account management, branch/tier/holiday configuration, audit log inspection, and audited administrative corrections.

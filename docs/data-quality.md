# Synthetic Dataset & Historical Data Quality Pipeline

## 1. Deterministic Synthetic Dataset (`scripts/seed_data.py`)

Using fixed deterministic seed `20261024` (`app/infrastructure/seed.py`), Pustakalaya generates a realistic multi-branch dataset for Lucknow, Uttar Pradesh:

| Entity | Seeded Count | Notes |
| :--- | :---: | :--- |
| Library Branches | **3** | `LKO-HZR` (Hazratganj Central), `LKO-GMN` (Gomti Nagar Civic), `LKO-ALG` (Aliganj Neighbourhood) |
| Membership Tiers | **3** | `BASIC`, `STANDARD`, `PREMIUM` |
| Holidays | **16** | Civic, national, and regional Lucknow closures across 2025–2026 |
| Catalog Authors | **140** | Hindi, Urdu, English, and academic authors (single- and multi-author works) |
| Catalog Books | **2,000** | Valid ISBN-13 / ISBN-10 titles plus intentional valid unnumbered (`isbn=NULL`) heritage editions |
| Physical Copies | **4,200** | Distributed across all 3 branches (`AVAILABLE`, `DAMAGED`, `LOST`, `WITHDRAWN`, plus `ON_LOAN`/`RESERVED` after import) |
| Registered Members | **500** | `MEM-000001`–`MEM-000500` across `ACTIVE`, `SUSPENDED`, `EXPIRED`, `CANCELLED` |
| Historical Source Fixture | **1,417** | Written to `instance/historical_source_records.json` over a 1-year window (`2025-10-24`–`2026-10-24`; earliest loan checkout `2025-11-03`) |

### Source vs. Operational Count Reconciliation

| Category | Source Records (`historical_source_records.json`) | Batch #1 Result (Initial Import) | Batch #2+ Result (Repeated Import) | Resulting Operational DB Rows |
| :--- | :---: | :---: | :---: | :---: |
| **`BOOK` rows** | 40 | 0 accepted, 11 rejected, 10 quarantined, 19 duplicate-skipped (ISBN already in 2,000 seeded books) | 0 accepted, 11 rejected, 10 quarantined, 19 duplicate-skipped | **2,000** `books` (`1,940` ISBN + `60` non-ISBN) |
| **`LOAN` rows** | 1,331 | 1,235 accepted, 70 rejected, 22 quarantined, 4 duplicate-skipped | 0 accepted, 70 rejected, 22 quarantined, 1,239 duplicate-skipped | **1,235** `loans` (`95` active, `1,140` returned), **253** `fines`, **201** `fine_payments` |
| **`RESERVATION` rows** | 46 | 40 accepted, 6 rejected, 0 quarantined, 0 duplicate-skipped | 0 accepted, 6 rejected, 0 quarantined, 40 duplicate-skipped | **40** `reservations` (`14` waiting, `20` fulfilled, `4` cancelled, `2` expired after hold processing) |
| **Total** | **1,417** | **1,275** accepted, **87** rejected, **32** quarantined, **23** duplicate-skipped, **0** errors | **0** accepted, **87** rejected, **32** quarantined, **1,298** duplicate-skipped, **0** errors | Reconciled 100% (`1,275 + 87 + 32 + 23 = 1,417`) |

## 2. Historical Import Validation & Quarantine (`DataImportService`)

Legacy library records are stored in `instance/historical_source_records.json`—completely separate from operational SQLite tables—and processed through `DataImportService` (`scripts/import_history.py` or `/data-quality`).

Each record is evaluated inside a database `SAVEPOINT` (`session.begin_nested()`):

### Dispositions
- **`ACCEPTED`**: Structurally and relationally valid; committed to operational tables with `source_reference` tracking.
- **`QUARANTINED`**: Plausible legacy record with an operational ambiguity that requires human librarian review rather than silent data fabrication.
- **`REJECTED`**: Violates hard structural, referential, chronological, or arithmetic invariants.
- **`DUPLICATE_SKIPPED`**: Duplicate `source_id` within the same file or already imported in a prior batch (`source_reference` / catalog ISBN check).

### Classification Codes Exercised by the Historical Fixture

| Issue Code | Disposition | Rule Enforced |
| :--- | :---: | :--- |
| `MALFORMED_ISBN` | `REJECTED` | Fails ISBN-10 or ISBN-13 check-digit validation |
| `AMBIGUOUS_CATALOG_RECORD_NO_ISBN` | `QUARANTINED` | Missing ISBN on a title matching an existing catalog book; held for librarian review instead of blind merging |
| `DUPLICATE_SOURCE_ID` | `DUPLICATE_SKIPPED` | Repeated `source_id` inside the import payload |
| `ALREADY_IMPORTED_SOURCE_REF` | `DUPLICATE_SKIPPED` | `source_id` already present in `loans.source_reference` or `reservations.source_reference` |
| `INVALID_MEMBER_REFERENCE` | `REJECTED` | References a nonexistent `member_code` |
| `INVALID_BRANCH_REFERENCE` | `REJECTED` | References a nonexistent `branch_code` |
| `INVALID_COPY_REFERENCE` | `REJECTED` | References a nonexistent `copy_accession` |
| `MISSING_RETURN_DATE` | `QUARANTINED` | Loan marked `RETURNED` lacks `return_date`; Pustakalaya refuses to invent historical timestamps |
| `RETURN_BEFORE_CHECKOUT` | `REJECTED` | `return_date < checkout_date` |
| `DUE_BEFORE_CHECKOUT` | `REJECTED` | `due_date < checkout_date` |
| `CONFLICTING_ACTIVE_LOAN` | `REJECTED` | Attempts to import a second `ACTIVE` loan on an already checked-out/unavailable copy |
| `OVERLAPPING_LOAN_HISTORY` | `REJECTED` | Historical checkout/return window overlaps an existing loan on the same physical copy |
| `EXCESSIVE_RENEWAL_COUNT` | `QUARANTINED` | `renewal_count` exceeds the member tier's `max_renewals` |
| `NEGATIVE_FINE_AMOUNT` | `REJECTED` | Negative fine assessment or payment |
| `OVERPAYMENT_EXCEEDS_ASSESSED_FINE` | `REJECTED` | `paid_amount > assessed_amount` |
| `FINE_ON_UNRETURNED_LOAN` | `QUARANTINED` | Finalized historical fine attached to an active unreturned loan |
| `UNEXPLAINED_FINE_DISCREPANCY` | `QUARANTINED` | Legacy `assessed_amount` disagrees with holiday/grace tier policy without `legacy_rate_verified=true` |
| `CONFLICTING_RESERVATION` | `REJECTED` | Duplicate active reservation for the same `(member, book)` or unavailable hold copy |
| `DUPLICATE_CATALOG_ISBN` | `DUPLICATE_SKIPPED` | Legacy `BOOK` record carries an ISBN already present in `books` |

Additional structural guard codes enforced by `DataImportService` on arbitrary external JSON inputs include `NON_OBJECT_RECORD`, `MISSING_SOURCE_ID`, `UNSUPPORTED_RECORD_TYPE`, `MISSING_TITLE`, `DUPLICATE_CATALOG_TITLE_NO_ISBN`, `MISSING_OR_INVALID_CHECKOUT_DATE`, `INVALID_DUE_DATE`, `INVALID_RETURN_DATE`, `UNSUPPORTED_LOAN_STATUS`, `INVALID_FINE_DECIMAL`, `BOOK_NOT_FOUND`, `MISSING_RESERVATION_DATE`, and `UNSUPPORTED_RESERVATION_STATUS`.

## 3. Idempotency Guarantee

Running `python scripts/import_history.py` a second time against the same database accepts **0** duplicate records, skips all previously imported `source_id` references cleanly, and leaves operational counts (`loans`, `reservations`, `fines`, `fine_payments`, `book_copies`) unchanged.

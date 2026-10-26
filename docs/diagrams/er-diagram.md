# Entity-Relationship Diagram

This Mermaid ER diagram documents the core circulation and catalog relationships in the SQLAlchemy schema. It intentionally emphasizes the entities central to the stated requirements; audit, authentication, import-batch, and import-issue support tables are described in the architecture/data-quality documentation and should be consulted in `app/models/orm.py` for their complete column-level schema.

```mermaid
erDiagram
    BRANCHES ||--o{ BOOK_COPIES : owns
    BRANCHES o|--o{ MEMBERS : home_branch
    BRANCHES ||--o{ LIBRARY_HOLIDAYS : defines

    BOOKS ||--o{ BOOK_COPIES : has
    BOOKS ||--o{ BOOK_AUTHORS : links
    AUTHORS ||--o{ BOOK_AUTHORS : links

    MEMBERSHIP_TIERS ||--o{ MEMBERS : governs

    MEMBERS ||--o{ LOANS : borrows
    BOOK_COPIES ||--o{ LOANS : loan_history

    MEMBERS ||--o{ RESERVATIONS : places
    BOOKS ||--o{ RESERVATIONS : requested_title
    BOOK_COPIES o|--o{ RESERVATIONS : allocated_copy

    LOANS ||--o{ FINES : may_generate
    FINES ||--o{ FINE_PAYMENTS : paid_by

    BRANCHES {
        int id PK
        string code UK
        string name UK
        string address
        boolean is_active
    }
    BOOKS {
        int id PK
        string title
        string isbn UK "nullable"
        string publisher
        int publication_year
        string language
        string category
    }
    AUTHORS {
        int id PK
        string full_name
        string normalized_name UK
    }
    BOOK_AUTHORS {
        int book_id PK,FK
        int author_id PK,FK
        int author_order
    }
    BOOK_COPIES {
        int id PK
        int book_id FK
        int owning_branch_id FK
        string accession_number UK
        string status
        date acquisition_date
    }
    MEMBERSHIP_TIERS {
        int id PK
        string code UK
        int max_active_loans
        int loan_duration_days
        int max_renewals
        int max_active_reservations
        int grace_period_days
        int daily_fine_rate
        int max_fine_per_loan
        int fine_blocking_threshold
    }
    MEMBERS {
        int id PK
        string member_code UK
        int home_branch_id FK "nullable"
        int tier_id FK
        string full_name
        string email UK "nullable"
        string status
        date registration_date
        date expiry_date "nullable"
    }
    LIBRARY_HOLIDAYS {
        int id PK
        int branch_id FK "nullable for network-wide holiday"
        date holiday_date
        string name
    }
    LOANS {
        int id PK
        int member_id FK
        int copy_id FK
        string status
        datetime checked_out_at
        date due_date
        datetime returned_at "nullable"
    }
    RESERVATIONS {
        int id PK
        int member_id FK
        int book_id FK
        int allocated_copy_id FK "nullable"
        string status
        datetime created_at
        datetime pickup_deadline_at "nullable"
    }
    FINES {
        int id PK
        int loan_id FK
        int assessed_amount "paise"
        int paid_amount "paise"
        string status
    }
    FINE_PAYMENTS {
        int id PK
        int fine_id FK
        int amount "paise"
        string payment_method
    }
```

## Relationship notes

- `BOOKS` represent catalog works/editions; `BOOK_COPIES` represent individual physical items.
- `BOOK_AUTHORS` is the many-to-many association table and preserves author order.
- A member belongs to one membership tier; a member may have many loans and reservations over time.
- A loan references one physical copy. A partial unique index in the implementation prevents more than one active loan per copy.
- Reservations target a catalog book and may temporarily allocate a physical copy for pickup.
- Fine amounts use integer paise in the SQLite schema to avoid floating-point currency errors.
- Nullable foreign keys are marked as such above. Confirm every column and constraint against the actual migration before using this as a database administration reference.

See [architecture.md](../architecture.md), [business-rules.md](../business-rules.md), and [data-quality.md](../data-quality.md).

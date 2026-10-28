# Architecture and Design Patterns

## System boundary

Pustakalaya is a local-first Flask application. The documented request path is browser -> Flask blueprints/templates -> application services -> repositories/transaction helpers -> SQLAlchemy models -> SQLite. Alembic manages schema evolution. Operational scripts support initialization, historical imports, reservation processing, and data verification.

## Architectural patterns to verify against source

| Pattern / decision | Expected evidence | Purpose |
|---|---|---|
| Layered architecture | app/web/, app/services/, app/repositories/, app/models/, app/domain/ | Separates HTTP, use cases, persistence, and domain rules. |
| Flask Blueprint modularization | Blueprint definitions and registration under app/web/ | Organizes routes by functional area. |
| Repository | Concrete classes under app/repositories/ and service call sites | Centralizes persistence queries behind domain-oriented operations. |
| Unit of Work / transaction boundary | Transaction helpers under app/infrastructure/ and service call sites | Groups related writes so failures can roll back consistently. |
| Domain policies and state machines | Types under app/domain/ and their call sites | Makes rules and allowed state transitions explicit. |
| ORM / Data Mapper | SQLAlchemy mappings under app/models/ | Maps relational data to Python objects. |
| Schema migration | Alembic revisions under migrations/ | Tracks incremental schema changes. |
| Application Service | app/services/ functions/classes called by routes and scripts | Shares use-case logic across UI and command-line workflows. |

These are architectural candidates based on the documented project layout, not proof that every pattern is implemented correctly. For an evaluation, cite the exact source file, class/function, a short real code excerpt, the caller/callee relationship, the design rationale, and a test where available. Remove any pattern that the source does not substantiate. Do not claim Singleton, Factory, Strategy, Observer, or another named pattern merely because a class has a similar name.

## Concrete workflow examples

### Issuing a physical copy
Documented behavior: validate member eligibility and tier rules; validate copy state; calculate the due date; prevent two requests from checking out the same copy; record the loan and update copy state; commit the related changes atomically. Cite the actual service, repository/transaction code, and concurrency test before claiming exact implementation details.

### Reservation allocation
Documented behavior: check eligibility and duplicate reservations; order reservations deterministically using FIFO; allocate a newly available copy; establish a pickup deadline; process expired holds without duplicating side effects. Cite the actual service and tests.

## Why these choices matter

- Services centralize multi-step use cases instead of duplicating rules in routes.
- Domain policies/state transitions make business rules explicit and testable.
- Repositories keep persistence queries separate from request handling.
- Transaction boundaries protect workflows that modify related records.
- Migrations make database changes reproducible across installations.

## Limitations

The project is documented as a single-host SQLite application. WAL mode and transactions help local concurrency and consistency but do not make SQLite a multi-node database service. The default server bind is loopback; network exposure requires deliberate deployment hardening.

## Evidence checklist for the submitted report

1. Pattern name and role in this project.
2. Exact source path and class/function name.
3. A brief excerpt copied from the actual source.
4. The component that calls it and what it delegates to.
5. The concrete problem it solves.
6. An existing test and its actual result, when available.

Never invent source examples, tests, performance numbers, or claims.
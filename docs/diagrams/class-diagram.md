# UML Class Diagram

This diagram summarizes the principal domain and application classes documented in the current codebase. It is a high-level view, not a replacement for the Python source.

```mermaid
classDiagram
direction TB

class LibraryBranch {
  +id
  +code
  +name
  +is_active
}
class Book {
  +id
  +title
  +isbn
  +publisher
  +publication_year
}
class Author {
  +id
  +full_name
  +normalized_name
}
class BookCopy {
  +id
  +accession_number
  +status
  +acquisition_date
  +transition_to()
  +assert_can_issue()
}
class Member {
  +id
  +member_code
  +status
  +registration_date
  +expiry_date
}
class MembershipTier {
  +code
  +max_active_loans
  +loan_duration_days
  +max_renewals
  +daily_fine_rate
  +max_fine_per_loan
}
class Loan {
  +id
  +status
  +checked_out_at
  +due_date
  +returned_at
}
class Reservation {
  +id
  +status
  +created_at
  +pickup_deadline_at
}
class Fine {
  +id
  +assessed_amount
  +paid_amount
  +status
}
class FinePayment {
  +id
  +amount
  +payment_method
}
class LibraryHoliday {
  +id
  +holiday_date
  +name
}
class User {
  +id
  +username
  +role
  +is_active
}
class AuditEvent {
  +id
  +event_type
  +created_at
}
class CirculationService {
  +issue_copy()
  +return_copy()
  +renew_loan()
}
class ReservationService {
  +create_reservation()
  +cancel_reservation()
  +process_expired_holds()
}
class FineService {
  +record_payment()
  +waive_fine()
}
class ReportingService {
  +overdue_report()
}
class BorrowingPolicyStrategy {
  <<interface>>
  +validate_eligibility()
}
class FineCalculationStrategy {
  <<interface>>
  +calculate_fine()
}
class PolicyFactory {
  +borrowing_policy()
  +fine_policy()
}
class BookRepository
class CopyRepository
class MemberRepository
class LoanRepository
class ReservationRepository
class FineRepository

Book "1" -- "0..*" BookCopy : has physical copies
Book "*" -- "*" Author : via BookAuthor
LibraryBranch "1" -- "0..*" BookCopy : owns
LibraryBranch "1" -- "0..*" LibraryHoliday : may define
MembershipTier "1" -- "0..*" Member : governs
Member "1" -- "0..*" Loan : borrows
BookCopy "1" -- "0..*" Loan : loan history
Member "1" -- "0..*" Reservation : places
Book "1" -- "0..*" Reservation : requested title
Loan "1" -- "0..*" Fine : may incur
Fine "1" -- "0..*" FinePayment : receives
User "1" -- "0..*" AuditEvent : performs

CirculationService ..> BookCopy
CirculationService ..> Member
CirculationService ..> LoanRepository
CirculationService ..> CopyRepository
CirculationService ..> MemberRepository
ReservationService ..> ReservationRepository
ReservationService ..> CopyRepository
FineService ..> FineRepository
ReportingService ..> LoanRepository
PolicyFactory ..> BorrowingPolicyStrategy : creates
PolicyFactory ..> FineCalculationStrategy : creates
BorrowingPolicyStrategy <|.. TierBorrowingPolicy
FineCalculationStrategy <|.. HolidayAwareFinePolicy
```

## Pattern and boundary notes

- **Strategy:** borrowing eligibility and fine calculation are policy interfaces with replaceable implementations.
- **Factory:** `PolicyFactory` centralizes selection/construction of policy implementations.
- **Service/repository separation:** services orchestrate use cases and transactions; repositories encapsulate persistence queries.
- **Unit of Work / transaction boundary:** write operations are coordinated by the infrastructure transaction helpers so state changes and audit events commit or roll back together.

For implementation details, see [architecture.md](../architecture.md) and [business-rules.md](../business-rules.md). If a class or method is renamed in the code, update this diagram to match.

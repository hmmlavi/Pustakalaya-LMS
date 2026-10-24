"""Domain exception hierarchy for Pustakalaya."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all expected domain and business-rule exceptions."""

    code: str = "DOMAIN_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class NotFoundError(DomainError):
    """Raised when a requested domain record cannot be found."""

    code = "NOT_FOUND"


class MemberNotFoundError(NotFoundError):
    code = "MEMBER_NOT_FOUND"


class BookNotFoundError(NotFoundError):
    code = "BOOK_NOT_FOUND"


class AuthorNotFoundError(NotFoundError):
    code = "AUTHOR_NOT_FOUND"


class CopyNotFoundError(NotFoundError):
    code = "COPY_NOT_FOUND"


class BranchNotFoundError(NotFoundError):
    code = "BRANCH_NOT_FOUND"


class LoanNotFoundError(NotFoundError):
    code = "LOAN_NOT_FOUND"


class ReservationNotFoundError(NotFoundError):
    code = "RESERVATION_NOT_FOUND"


class FineNotFoundError(NotFoundError):
    code = "FINE_NOT_FOUND"


class UserNotFoundError(NotFoundError):
    code = "USER_NOT_FOUND"


class EligibilityError(DomainError):
    """Raised when a member does not satisfy borrowing or reservation policies."""

    code = "ELIGIBILITY_ERROR"


class MemberNotEligibleError(EligibilityError):
    code = "MEMBER_NOT_ELIGIBLE"


class BorrowingLimitExceededError(EligibilityError):
    code = "BORROWING_LIMIT_EXCEEDED"


class OutstandingFineError(EligibilityError):
    code = "OUTSTANDING_FINE_BLOCK"


class CirculationError(DomainError):
    """Raised when a circulation or copy-state operation is disallowed."""

    code = "CIRCULATION_ERROR"


class CopyUnavailableError(CirculationError):
    code = "COPY_UNAVAILABLE"


class CopyReservedError(CirculationError):
    code = "COPY_RESERVED"


class InvalidStateTransitionError(CirculationError):
    code = "INVALID_STATE_TRANSITION"


class LoanAlreadyReturnedError(CirculationError):
    code = "LOAN_ALREADY_RETURNED"


class RenewalNotAllowedError(CirculationError):
    code = "RENEWAL_NOT_ALLOWED"


class RenewalLimitExceededError(CirculationError):
    code = "RENEWAL_LIMIT_EXCEEDED"


class ReservationConflictError(DomainError):
    code = "RESERVATION_CONFLICT"


class InvalidFinePaymentError(DomainError):
    code = "INVALID_FINE_PAYMENT"


class CatalogValidationError(DomainError):
    code = "CATALOG_VALIDATION_ERROR"


class InvalidISBNError(CatalogValidationError):
    code = "INVALID_ISBN"


class DuplicateRecordError(CatalogValidationError):
    code = "DUPLICATE_RECORD"


class DataImportValidationError(DomainError):
    code = "DATA_IMPORT_VALIDATION_ERROR"


class AuthenticationError(DomainError):
    code = "AUTHENTICATION_FAILED"


class AuthorizationError(DomainError):
    code = "AUTHORIZATION_DENIED"

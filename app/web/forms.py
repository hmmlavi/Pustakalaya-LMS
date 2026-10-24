"""Server-validated Flask-WTF forms with CSRF protection across all modules."""

from __future__ import annotations

from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateField,
    DecimalField,
    HiddenField,
    IntegerField,
    PasswordField,
    SelectField,
    StringField,
    TextAreaField,
)
from wtforms.fields.choices import SelectChoice
from wtforms.validators import (
    DataRequired,
    EqualTo,
    InputRequired,
    Length,
    NumberRange,
    Optional,
    ValidationError as WTFormsValidationError,
)

from app.domain.value_objects import normalize_email


def OfflineEmail():
    """Offline deterministic email format validator backed by domain normalize_email."""

    def _validator(_form, field):
        if not field.data or not str(field.data).strip():
            return
        try:
            normalize_email(str(field.data))
        except ValueError as exc:
            raise WTFormsValidationError(str(exc)) from exc

    return _validator


class ActionCSRFForm(FlaskForm):
    """Minimal CSRF-validated form for single-button POST actions."""


class LoginForm(FlaskForm):
    username = StringField(
        "Username",
        validators=[DataRequired(message="Enter your staff username."), Length(max=60)],
    )
    password = PasswordField(
        "Password",
        validators=[DataRequired(message="Enter your password."), Length(max=128)],
    )


class FirstRunSetupForm(FlaskForm):
    username = StringField(
        "Administrator Username",
        validators=[
            DataRequired(message="Choose an administrator username."),
            Length(min=3, max=60),
        ],
    )
    full_name = StringField(
        "Full Name",
        validators=[DataRequired(message="Enter full name."), Length(min=2, max=120)],
    )
    email = StringField(
        "Email Address (Optional)",
        validators=[Optional(), OfflineEmail(), Length(max=160)],
    )
    password = PasswordField(
        "Password (min 8 characters)",
        validators=[
            DataRequired(),
            Length(min=8, max=128, message="Password must be at least 8 characters."),
        ],
    )
    confirm_password = PasswordField(
        "Confirm Password",
        validators=[
            DataRequired(),
            EqualTo("password", message="Passwords must match."),
        ],
    )


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField(
        "Current Password", validators=[DataRequired(), Length(min=1, max=128)]
    )
    new_password = PasswordField(
        "New Password",
        validators=[
            DataRequired(),
            Length(min=8, max=128, message="New password must be at least 8 characters."),
        ],
    )
    confirm_password = PasswordField(
        "Confirm New Password",
        validators=[
            DataRequired(),
            EqualTo("new_password", message="New passwords must match."),
        ],
    )


class BookForm(FlaskForm):
    title = StringField("Title", validators=[DataRequired(), Length(min=1, max=255)])
    subtitle = StringField("Subtitle", validators=[Optional(), Length(max=255)])
    isbn = StringField(
        "ISBN (ISBN-10 or ISBN-13, optional)",
        validators=[Optional(), Length(max=20)],
    )
    author_names = StringField(
        "Authors (comma-separated)",
        validators=[DataRequired(message="Provide at least one author."), Length(max=500)],
    )
    publisher = StringField(
        "Publisher", validators=[DataRequired(), Length(min=1, max=160)]
    )
    publication_year = IntegerField(
        "Publication Year",
        validators=[DataRequired(), NumberRange(min=1450, max=2100)],
    )
    category = StringField(
        "Category / Subject", validators=[DataRequired(), Length(min=1, max=80)]
    )
    language = StringField(
        "Language",
        default="English",
        validators=[DataRequired(), Length(min=1, max=50)],
    )
    edition = StringField("Edition", validators=[Optional(), Length(max=60)])
    description = TextAreaField(
        "Description", validators=[Optional(), Length(max=2000)]
    )


class AuthorForm(FlaskForm):
    full_name = StringField(
        "Full Name", validators=[DataRequired(), Length(min=1, max=160)]
    )
    birth_year = IntegerField(
        "Birth Year", validators=[Optional(), NumberRange(min=500, max=2100)]
    )
    biography = TextAreaField("Biography", validators=[Optional(), Length(max=2000)])


class BookCopyForm(FlaskForm):
    book_id = IntegerField(
        "Catalog Book ID", validators=[DataRequired(), NumberRange(min=1)]
    )
    owning_branch_id = SelectField("Owning Branch", coerce=int, validators=[DataRequired()])
    accession_number = StringField(
        "Inventory / Accession Number",
        validators=[DataRequired(), Length(min=2, max=40)],
    )
    acquisition_date = DateField("Acquisition Date", validators=[Optional()])
    status = SelectField(
        "Initial Status",
        choices=[
            SelectChoice("AVAILABLE", "AVAILABLE"),
            SelectChoice("DAMAGED", "DAMAGED"),
            SelectChoice("LOST", "LOST"),
            SelectChoice("WITHDRAWN", "WITHDRAWN"),
        ],
        default="AVAILABLE",
        validators=[DataRequired()],
    )
    condition_notes = TextAreaField(
        "Condition Notes", validators=[Optional(), Length(max=1000)]
    )


class CopyStatusForm(FlaskForm):
    new_status = SelectField(
        "New Copy Status",
        choices=[
            SelectChoice("AVAILABLE", "AVAILABLE"),
            SelectChoice("DAMAGED", "DAMAGED"),
            SelectChoice("LOST", "LOST"),
            SelectChoice("WITHDRAWN", "WITHDRAWN"),
        ],
        validators=[DataRequired()],
    )
    note = StringField("Status Change Note", validators=[Optional(), Length(max=300)])


class MemberForm(FlaskForm):
    full_name = StringField(
        "Full Name", validators=[DataRequired(), Length(min=2, max=160)]
    )
    tier_code = SelectField(
        "Membership Tier",
        choices=[
            SelectChoice("BASIC", "BASIC — Basic Citizen"),
            SelectChoice("STANDARD", "STANDARD — Standard Reader"),
            SelectChoice("PREMIUM", "PREMIUM — Premium Scholar"),
        ],
        default="STANDARD",
        validators=[DataRequired()],
    )
    email = StringField(
        "Email Address", validators=[Optional(), OfflineEmail(), Length(max=254)]
    )
    phone = StringField("Phone Number", validators=[Optional(), Length(max=25)])
    home_branch_id = SelectField(
        "Home Branch", coerce=int, validators=[Optional()]
    )
    expiry_date = DateField("Membership Expiry Date", validators=[Optional()])
    address = TextAreaField("Address", validators=[Optional(), Length(max=300)])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=1000)])


class MemberStatusForm(FlaskForm):
    new_status = SelectField(
        "Membership Status",
        choices=[
            SelectChoice("ACTIVE", "ACTIVE"),
            SelectChoice("SUSPENDED", "SUSPENDED"),
            SelectChoice("EXPIRED", "EXPIRED"),
            SelectChoice("CANCELLED", "CANCELLED"),
        ],
        validators=[DataRequired()],
    )
    reason = StringField(
        "Reason for Status Change", validators=[Optional(), Length(max=250)]
    )


class IssueCopyForm(FlaskForm):
    member_identifier = StringField(
        "Member ID or Code (e.g. MEM-000001)",
        validators=[DataRequired(), Length(min=1, max=40)],
    )
    copy_identifier = StringField(
        "Copy Accession Number (e.g. LKO-HZR-000001)",
        validators=[DataRequired(), Length(min=1, max=40)],
    )
    issuing_branch_id = SelectField(
        "Issuing Branch", coerce=int, validators=[Optional()]
    )


class ReturnCopyForm(FlaskForm):
    identifier = StringField(
        "Copy Accession Number or Loan Number",
        validators=[DataRequired(), Length(min=1, max=50)],
    )
    returned_branch_id = SelectField(
        "Receiving Branch", coerce=int, validators=[Optional()]
    )
    returned_damaged = BooleanField("Copy returned in damaged condition")
    return_notes = StringField(
        "Return / Condition Notes", validators=[Optional(), Length(max=300)]
    )


class ReportLostForm(FlaskForm):
    notes = StringField(
        "Lost Copy Report Details",
        validators=[DataRequired(), Length(min=3, max=300)],
    )


class ReservationCreateForm(FlaskForm):
    member_identifier = StringField(
        "Member ID or Code (e.g. MEM-000001)",
        validators=[DataRequired(), Length(min=1, max=40)],
    )
    book_id = IntegerField(
        "Catalog Book ID", validators=[DataRequired(), NumberRange(min=1)]
    )
    preferred_branch_id = SelectField(
        "Preferred Pickup Branch", coerce=int, validators=[Optional()]
    )
    notes = StringField("Notes", validators=[Optional(), Length(max=250)])


class ReservationCancelForm(FlaskForm):
    reason = StringField(
        "Cancellation Reason", validators=[Optional(), Length(max=200)]
    )


class FinePaymentForm(FlaskForm):
    amount = DecimalField(
        "Payment Amount (INR)",
        places=2,
        validators=[
            DataRequired(message="Enter a valid INR payment amount."),
            NumberRange(min=Decimal("0.01"), max=Decimal("100000.00")),
        ],
    )
    payment_method = SelectField(
        "Payment Method",
        choices=[
            SelectChoice("CASH", "Cash"),
            SelectChoice("UPI", "UPI"),
            SelectChoice("CARD", "Debit / Credit Card"),
            SelectChoice("BANK_TRANSFER", "Bank Transfer"),
        ],
        default="CASH",
        validators=[DataRequired()],
    )
    branch_id = SelectField("Collecting Branch", coerce=int, validators=[Optional()])
    reference_note = StringField(
        "Transaction / UPI Reference", validators=[Optional(), Length(max=200)]
    )
    idempotency_key = HiddenField(validators=[Optional(), Length(max=80)])


class FineWaiveForm(FlaskForm):
    reason = StringField(
        "Administrative Waiver Reason",
        validators=[DataRequired(), Length(min=5, max=250)],
    )


class StaffUserCreateForm(FlaskForm):
    username = StringField(
        "Username", validators=[DataRequired(), Length(min=3, max=60)]
    )
    full_name = StringField(
        "Full Name", validators=[DataRequired(), Length(min=2, max=160)]
    )
    email = StringField("Email", validators=[Optional(), OfflineEmail(), Length(max=254)])
    role = SelectField(
        "Role",
        choices=[
            SelectChoice("ADMIN", "Administrator"),
            SelectChoice("LIBRARIAN", "Librarian"),
            SelectChoice("STAFF_READONLY", "Read-Only Staff"),
        ],
        default="LIBRARIAN",
        validators=[DataRequired()],
    )
    branch_id = SelectField("Assigned Branch", coerce=int, validators=[Optional()])
    password = PasswordField(
        "Initial Password", validators=[DataRequired(), Length(min=8, max=128)]
    )


class StaffUserUpdateForm(FlaskForm):
    role = SelectField(
        "Role",
        choices=[
            SelectChoice("ADMIN", "Administrator"),
            SelectChoice("LIBRARIAN", "Librarian"),
            SelectChoice("STAFF_READONLY", "Read-Only Staff"),
        ],
        validators=[DataRequired()],
    )
    branch_id = SelectField("Assigned Branch", coerce=int, validators=[Optional()])
    is_active = BooleanField("Active Account")
    new_password = PasswordField(
        "Reset Password (optional)",
        validators=[Optional(), Length(min=8, max=128)],
    )


class BranchForm(FlaskForm):
    code = StringField("Branch Code", validators=[DataRequired(), Length(min=3, max=20)])
    name = StringField("Branch Name", validators=[DataRequired(), Length(min=3, max=120)])
    address = StringField("Address", validators=[DataRequired(), Length(min=5, max=300)])
    pincode = StringField("PIN Code", validators=[DataRequired(), Length(min=5, max=10)])
    phone = StringField("Contact Phone", validators=[DataRequired(), Length(min=7, max=30)])
    email = StringField(
        "Contact Email", validators=[DataRequired(), OfflineEmail(), Length(max=150)]
    )
    is_active = BooleanField("Branch Open & Active", default=True)


class BranchUpdateForm(FlaskForm):
    name = StringField("Branch Name", validators=[DataRequired(), Length(min=3, max=120)])
    address = StringField("Address", validators=[DataRequired(), Length(min=5, max=300)])
    pincode = StringField("PIN Code", validators=[DataRequired(), Length(min=5, max=10)])
    phone = StringField("Contact Phone", validators=[DataRequired(), Length(min=7, max=30)])
    email = StringField(
        "Contact Email", validators=[DataRequired(), OfflineEmail(), Length(max=150)]
    )
    is_active = BooleanField("Branch Open & Active", default=True)


class TierPolicyForm(FlaskForm):
    max_active_loans = IntegerField(
        "Max Simultaneous Loans",
        validators=[InputRequired(), NumberRange(min=0, max=50)],
    )
    loan_duration_days = IntegerField(
        "Standard Loan Days",
        validators=[InputRequired(), NumberRange(min=1, max=180)],
    )
    max_renewals = IntegerField(
        "Max Renewals", validators=[InputRequired(), NumberRange(min=0, max=20)]
    )
    renewal_duration_days = IntegerField(
        "Renewal Days",
        validators=[InputRequired(), NumberRange(min=1, max=90)],
    )
    max_active_reservations = IntegerField(
        "Max Active Reservations",
        validators=[InputRequired(), NumberRange(min=0, max=30)],
    )
    grace_period_days = IntegerField(
        "Grace Period Days", validators=[InputRequired(), NumberRange(min=0, max=30)]
    )
    daily_fine_rate = DecimalField(
        "Daily Fine Rate (INR)",
        places=2,
        validators=[
            InputRequired(),
            NumberRange(min=Decimal("0.00"), max=Decimal("1000.00")),
        ],
    )
    max_fine_per_loan = DecimalField(
        "Max Fine Cap per Loan (INR)",
        places=2,
        validators=[
            InputRequired(),
            NumberRange(min=Decimal("0.00"), max=Decimal("50000.00")),
        ],
    )
    fine_blocking_threshold = DecimalField(
        "Borrowing Block Threshold (INR)",
        places=2,
        validators=[
            InputRequired(),
            NumberRange(min=Decimal("0.00"), max=Decimal("50000.00")),
        ],
    )
    borrowing_enabled = BooleanField("Borrowing Enabled", default=True)


class HolidayForm(FlaskForm):
    holiday_date = DateField("Holiday Date", validators=[DataRequired()])
    name = StringField(
        "Holiday Name", validators=[DataRequired(), Length(min=2, max=140)]
    )
    branch_id = SelectField("Scope", coerce=int, validators=[Optional()])
    description = StringField(
        "Description", validators=[Optional(), Length(max=255)]
    )


class AdminReinstateCopyForm(FlaskForm):
    copy_id = StringField(
        "Copy ID or Accession Number",
        validators=[DataRequired(), Length(min=1, max=40)],
    )
    reason = StringField(
        "Administrative Justification",
        validators=[DataRequired(), Length(min=5, max=250)],
    )


class AdminAdjustDueDateForm(FlaskForm):
    loan_id = StringField(
        "Loan ID or Loan Number",
        validators=[DataRequired(), Length(min=1, max=40)],
    )
    new_due_date = DateField("Adjusted Due Date", validators=[DataRequired()])
    reason = StringField(
        "Administrative Justification",
        validators=[DataRequired(), Length(min=5, max=250)],
    )


class AppearancePreferencesForm(FlaskForm):
    theme_preference = SelectField(
        "Color Theme",
        choices=[
            SelectChoice("system", "Match Operating System"),
            SelectChoice("day", "Peaceful Botanical Library (Light)"),
            SelectChoice("night", "Moonlit Forest Library (Dark)"),
        ],
        default="system",
        validators=[DataRequired()],
    )
    ambience_mode = SelectField(
        "Library Ambience",
        choices=[
            SelectChoice("auto", "Auto — Follows Active Theme"),
            SelectChoice("minimal", "Minimal — Calm Stationary Glow"),
            SelectChoice("off", "Off — Disable Decorative Animations"),
        ],
        default="auto",
        validators=[DataRequired()],
    )

"""Authentication and staff user management service."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.domain.enums import AuditEventType, UserRole
from app.domain.exceptions import (
    AuthenticationError,
    AuthorizationError,
    DuplicateRecordError,
    UserNotFoundError,
)
from app.domain.value_objects import normalize_email, utc_now
from app.infrastructure.transactions import read_session, write_transaction
from app.models import User
from app.repositories import AuditRepository, BranchRepository, UserRepository


class AuthenticationService:
    def __init__(self, session_factory: sessionmaker) -> None:
        self.session_factory = session_factory

    def authenticate(self, username: str, password: str) -> User:
        if not username or not username.strip() or not password:
            raise AuthenticationError("Username and password are required.")
        with write_transaction(self.session_factory) as session:
            user_repo = UserRepository(session)
            user = user_repo.get_by_username(username.strip())
            if user is None or not user.verify_password(password):
                raise AuthenticationError("Invalid username or password.")
            if not user.is_active:
                raise AuthenticationError(
                    "This staff account is deactivated. Contact a library administrator."
                )
            user.last_login_at = utc_now()
            session.flush()
            return user

    def get_active_user(self, user_id: int) -> User | None:
        with read_session(self.session_factory) as session:
            user = UserRepository(session).get_by_id(user_id)
            if user is None or not user.is_active:
                return None
            return user

    def list_users(self) -> list[User]:
        with read_session(self.session_factory) as session:
            return list(UserRepository(session).list_all())

    def count_users(self) -> int:
        with read_session(self.session_factory) as session:
            return UserRepository(session).count_all()

    def bootstrap_initial_admin(
        self,
        *,
        username: str,
        full_name: str,
        password: str,
        email: str | None = None,
        must_change_password: bool = False,
    ) -> tuple[User, bool]:
        """Idempotently create an initial administrator account if username does not exist."""
        clean_username = username.strip().lower()
        if len(clean_username) < 3:
            raise ValueError("Administrator username must be at least 3 characters.")
        norm_email = normalize_email(email)

        with write_transaction(self.session_factory) as session:
            user_repo = UserRepository(session)
            existing = user_repo.get_by_username(clean_username)
            if existing is not None:
                return existing, False

            user = User(
                username=clean_username,
                full_name=full_name.strip() or "Chief Administrator",
                email=norm_email,
                password_hash="",
                role=UserRole.ADMIN.value,
                is_active=True,
                must_change_password=must_change_password,
            )
            user.set_password(password)
            user_repo.add(user)
            AuditRepository(session).record(
                event_type=AuditEventType.USER_MANAGED,
                entity_type="USER",
                entity_id=user.id,
                summary=f"Bootstrapped initial administrator '{user.username}'.",
                details={"username": user.username, "role": user.role},
            )
            return user, True

    def create_first_run_admin(
        self,
        *,
        username: str,
        full_name: str,
        password: str,
        email: str | None = None,
    ) -> User:
        """Atomically create the initial administrator ONLY when zero staff accounts exist."""
        clean_username = username.strip().lower()
        if len(clean_username) < 3:
            raise ValueError("Administrator username must be at least 3 characters.")
        norm_email = normalize_email(email)

        with write_transaction(self.session_factory) as session:
            user_repo = UserRepository(session)
            if user_repo.count_all() > 0:
                raise AuthorizationError(
                    "First-run setup is disabled because a staff account already exists."
                )
            user = User(
                username=clean_username,
                full_name=full_name.strip() or "Chief Library Administrator",
                email=norm_email,
                password_hash="",
                role=UserRole.ADMIN.value,
                is_active=True,
                must_change_password=False,
            )
            user.set_password(password)
            user_repo.add(user)
            AuditRepository(session).record(
                event_type=AuditEventType.USER_MANAGED,
                entity_type="USER",
                entity_id=user.id,
                summary=f"Completed first-run setup for initial administrator '{user.username}'.",
                details={"username": user.username, "role": user.role},
            )
            return user

    def create_staff_user(
        self,
        *,
        username: str,
        full_name: str,
        password: str,
        role: UserRole | str,
        email: str | None = None,
        branch_id: int | None = None,
        must_change_password: bool = False,
        actor_user_id: int | None = None,
    ) -> User:
        clean_username = username.strip().lower()
        if not clean_username or len(clean_username) < 3:
            raise ValueError("Username must be at least 3 characters long.")
        clean_name = full_name.strip()
        if not clean_name:
            raise ValueError("Full name is required.")
        role_enum = UserRole(role) if not isinstance(role, UserRole) else role
        norm_email = normalize_email(email)

        with write_transaction(self.session_factory) as session:
            user_repo = UserRepository(session)
            if user_repo.get_by_username(clean_username) is not None:
                raise DuplicateRecordError(
                    f"Username '{clean_username}' is already registered."
                )
            if norm_email and user_repo.get_by_email(norm_email) is not None:
                raise DuplicateRecordError(
                    f"Email '{norm_email}' is already assigned to another staff account."
                )
            if branch_id is not None:
                if BranchRepository(session).get_by_id(branch_id) is None:
                    raise ValueError("Selected branch does not exist.")

            user = User(
                username=clean_username,
                full_name=clean_name,
                email=norm_email,
                password_hash="",
                role=role_enum.value,
                branch_id=branch_id,
                is_active=True,
                must_change_password=must_change_password,
            )
            user.set_password(password)
            user_repo.add(user)
            AuditRepository(session).record(
                event_type=AuditEventType.USER_MANAGED,
                entity_type="USER",
                entity_id=user.id,
                actor_user_id=actor_user_id,
                branch_id=branch_id,
                summary=f"Created staff account '{user.username}' with role {user.role}.",
                details={"username": user.username, "role": user.role},
            )
            return user

    def change_own_password(
        self,
        user_id: int,
        *,
        current_password: str,
        new_password: str,
    ) -> User:
        with write_transaction(self.session_factory) as session:
            user = UserRepository(session).get_by_id(user_id)
            if user is None:
                raise UserNotFoundError(f"User #{user_id} was not found.")
            if not user.verify_password(current_password):
                raise AuthenticationError("Current password is incorrect.")
            user.set_password(new_password)
            user.must_change_password = False
            user.updated_at = utc_now()
            AuditRepository(session).record(
                event_type=AuditEventType.USER_MANAGED,
                entity_type="USER",
                entity_id=user.id,
                actor_user_id=user.id,
                summary=f"Password updated by user '{user.username}'.",
            )
            return user

    def update_staff_user(
        self,
        target_user_id: int,
        *,
        role: UserRole | str | None = None,
        is_active: bool | None = None,
        branch_id: int | None = None,
        new_password: str | None = None,
        actor_user_id: int | None = None,
    ) -> User:
        with write_transaction(self.session_factory) as session:
            user_repo = UserRepository(session)
            target = user_repo.get_by_id(target_user_id)
            if target is None:
                raise UserNotFoundError(f"User #{target_user_id} was not found.")

            new_role = (
                (UserRole(role) if not isinstance(role, UserRole) else role)
                if role is not None
                else target.role_enum
            )
            new_active = target.is_active if is_active is None else bool(is_active)

            # Prevent removing the last active administrator
            if target.role_enum == UserRole.ADMIN and (
                new_role != UserRole.ADMIN or not new_active
            ):
                active_admins = int(
                    session.scalar(
                        select(func.count(User.id)).where(
                            User.role == UserRole.ADMIN.value,
                            User.is_active.is_(True),
                        )
                    )
                    or 0
                )
                if active_admins <= 1:
                    raise AuthorizationError(
                        "Cannot deactivate or demote the last active administrator account."
                    )

            target.role = new_role.value
            target.is_active = new_active
            target.branch_id = branch_id
            if new_password and new_password.strip():
                target.set_password(new_password.strip())
            target.updated_at = utc_now()

            AuditRepository(session).record(
                event_type=AuditEventType.USER_MANAGED,
                entity_type="USER",
                entity_id=target.id,
                actor_user_id=actor_user_id,
                summary=f"Updated staff account '{target.username}' (role={target.role}, active={target.is_active}).",
                details={
                    "role": target.role,
                    "is_active": target.is_active,
                    "branch_id": target.branch_id,
                    "password_reset": bool(new_password and new_password.strip()),
                },
            )
            return target

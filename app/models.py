from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index(
            "uq_user_performer_person",
            "person_id",
            unique=True,
            postgresql_where=text("person_id IS NOT NULL AND merged_into_user_id IS NULL"),
            sqlite_where=text("person_id IS NOT NULL AND merged_into_user_id IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    person_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(150), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    merged_into_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    login_methods: Mapped[list["UserLoginMethod"]] = relationship(
        "UserLoginMethod", back_populates="user", cascade="all, delete-orphan",
        primaryjoin="User.id == UserLoginMethod.user_id", foreign_keys="UserLoginMethod.user_id"
    )
    role_assignments: Mapped[list["AccessRoleAssignment"]] = relationship(
        "AccessRoleAssignment", back_populates="user", cascade="all, delete-orphan",
        primaryjoin="User.id == AccessRoleAssignment.user_id", foreign_keys="AccessRoleAssignment.user_id"
    )


class UserLoginMethod(Base):
    __tablename__ = "user_login_methods"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    method_type: Mapped[str] = mapped_column(String(32), nullable=False)
    identifier: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    user: Mapped["User"] = relationship("User", back_populates="login_methods",
        primaryjoin="User.id == UserLoginMethod.user_id", foreign_keys="UserLoginMethod.user_id")


class OperatorPosition(Base):
    __tablename__ = "operator_positions"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class OperatorEmploymentPeriod(Base):
    __tablename__ = "operator_employment_periods"
    __table_args__ = (
        CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name="ck_operator_employment_period_dates",
        ),
        CheckConstraint(
            "status IN ('active', 'terminated')",
            name="ck_operator_employment_period_status",
        ),
        Index(
            "uq_operator_active_period",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index(
            "uq_operator_personnel_number",
            "personnel_number",
            unique=True,
            postgresql_where=text("personnel_number IS NOT NULL AND personnel_number <> ''"),
            sqlite_where=text("personnel_number IS NOT NULL AND personnel_number <> ''"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", name="fk_operator_employment_period_user"), nullable=False, index=True
    )
    personnel_number: Mapped[str | None] = mapped_column(String(250), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    termination_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="hr")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class OperatorPositionAssignment(Base):
    __tablename__ = "operator_position_assignments"
    __table_args__ = (
        CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name="ck_operator_position_assignment_dates",
        ),
        CheckConstraint(
            "status IN ('active', 'terminated')",
            name="ck_operator_position_assignment_status",
        ),
        Index(
            "uq_operator_active_position",
            "employment_period_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    employment_period_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("operator_employment_periods.id", name="fk_operator_position_employment_period"),
        nullable=False,
        index=True,
    )
    position_code: Mapped[str] = mapped_column(
        String(64), ForeignKey("operator_positions.code", name="fk_operator_position_code"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class ExternalOrganizationMembership(Base):
    __tablename__ = "external_organization_memberships"
    __table_args__ = (
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_external_membership_dates",
        ),
        CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_external_membership_status",
        ),
        Index(
            "uq_external_active_membership",
            "user_id",
            "organization_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", name="fk_external_membership_user"), nullable=False, index=True
    )
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AccessRoleAssignment(Base):
    __tablename__ = "access_role_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    role_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    granted_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    operator_employment_period_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("operator_employment_periods.id", name="fk_access_assignment_operator_period"),
        nullable=True,
        index=True,
    )
    operator_position_assignment_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("operator_position_assignments.id", name="fk_access_assignment_operator_position"),
        nullable=True,
        index=True,
    )
    membership_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("external_organization_memberships.id", name="fk_access_assignment_membership"),
        nullable=True,
        index=True,
    )
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="role_assignments",
        primaryjoin="User.id == AccessRoleAssignment.user_id", foreign_keys="AccessRoleAssignment.user_id")


class AccessEventInbox(Base):
    __tablename__ = "access_event_inbox"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class AccessConsumerState(Base):
    __tablename__ = "access_consumer_state"

    aggregate_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_version: Mapped[int] = mapped_column(Integer, nullable=False)
    last_event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ServiceClient(Base):
    __tablename__ = "service_clients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(150), unique=True, nullable=False, index=True)
    secret_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    service_name: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

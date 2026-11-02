"""own operator employment, positions and external memberships

Revision ID: 0003_operator_employment
Revises: 0002_person_linked_accounts
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_operator_employment"
down_revision: str | None = "0002_person_linked_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operator_positions",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("code"),
    )
    op.execute(
        sa.text(
            "INSERT INTO operator_positions (code, name, is_active) "
            "VALUES ('operator_software', 'Оператор ПО', TRUE) "
            "ON CONFLICT (code) DO NOTHING"
        )
    )

    op.create_table(
        "operator_employment_periods",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("personnel_number", sa.String(length=250), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("termination_reason", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="hr"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name="ck_operator_employment_period_dates",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'terminated')",
            name="ck_operator_employment_period_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_operator_employment_period_user",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operator_employment_periods_user_id",
        "operator_employment_periods",
        ["user_id"],
    )
    op.create_index(
        "uq_operator_active_period",
        "operator_employment_periods",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_operator_personnel_number",
        "operator_employment_periods",
        ["personnel_number"],
        unique=True,
        postgresql_where=sa.text("personnel_number IS NOT NULL AND personnel_number <> ''"),
    )

    op.create_table(
        "operator_position_assignments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("employment_period_id", sa.String(length=36), nullable=False),
        sa.Column("position_code", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name="ck_operator_position_assignment_dates",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'terminated')",
            name="ck_operator_position_assignment_status",
        ),
        sa.ForeignKeyConstraint(
            ["employment_period_id"],
            ["operator_employment_periods.id"],
            name="fk_operator_position_employment_period",
        ),
        sa.ForeignKeyConstraint(
            ["position_code"],
            ["operator_positions.code"],
            name="fk_operator_position_code",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operator_position_assignments_employment_period_id",
        "operator_position_assignments",
        ["employment_period_id"],
    )
    op.create_index(
        "uq_operator_active_position",
        "operator_position_assignments",
        ["employment_period_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "external_organization_memberships",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_external_membership_dates",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_external_membership_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_external_membership_user",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_external_organization_memberships_user_id",
        "external_organization_memberships",
        ["user_id"],
    )
    op.create_index(
        "ix_external_organization_memberships_organization_id",
        "external_organization_memberships",
        ["organization_id"],
    )
    op.create_index(
        "uq_external_active_membership",
        "external_organization_memberships",
        ["user_id", "organization_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.add_column(
        "access_role_assignments",
        sa.Column("operator_employment_period_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "access_role_assignments",
        sa.Column("operator_position_assignment_id", sa.String(length=36), nullable=True),
    )
    op.execute("UPDATE access_role_assignments SET membership_id = NULL WHERE membership_id IS NOT NULL")
    op.create_foreign_key(
        "fk_access_assignment_operator_period",
        "access_role_assignments",
        "operator_employment_periods",
        ["operator_employment_period_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_access_assignment_operator_position",
        "access_role_assignments",
        "operator_position_assignments",
        ["operator_position_assignment_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_access_assignment_membership",
        "access_role_assignments",
        "external_organization_memberships",
        ["membership_id"],
        ["id"],
    )
    op.create_index(
        "ix_access_role_assignments_operator_employment_period_id",
        "access_role_assignments",
        ["operator_employment_period_id"],
    )
    op.create_index(
        "ix_access_role_assignments_operator_position_assignment_id",
        "access_role_assignments",
        ["operator_position_assignment_id"],
    )
    op.create_index(
        "ix_access_role_assignments_membership_id",
        "access_role_assignments",
        ["membership_id"],
    )

    op.drop_column("access_role_assignments", "organization_id")
    op.drop_column("access_role_assignments", "employment_episode_id")
    op.drop_column("access_role_assignments", "position_assignment_id")

    op.drop_index("uq_users_canonical_person", table_name="users")
    op.create_index(
        "uq_user_performer_person",
        "users",
        ["person_id"],
        unique=True,
        postgresql_where=sa.text("person_id IS NOT NULL AND merged_into_user_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_user_performer_person", table_name="users")
    op.create_index(
        "uq_users_canonical_person",
        "users",
        ["person_id"],
        unique=True,
        postgresql_where=sa.text("person_id IS NOT NULL AND merged_into_user_id IS NULL AND is_active"),
    )

    op.add_column("access_role_assignments", sa.Column("organization_id", sa.String(length=36), nullable=True))
    op.add_column("access_role_assignments", sa.Column("employment_episode_id", sa.Integer(), nullable=True))
    op.add_column("access_role_assignments", sa.Column("position_assignment_id", sa.Integer(), nullable=True))
    op.drop_index("ix_access_role_assignments_membership_id", table_name="access_role_assignments")
    op.drop_index(
        "ix_access_role_assignments_operator_position_assignment_id",
        table_name="access_role_assignments",
    )
    op.drop_index(
        "ix_access_role_assignments_operator_employment_period_id",
        table_name="access_role_assignments",
    )
    op.drop_constraint("fk_access_assignment_membership", "access_role_assignments", type_="foreignkey")
    op.drop_constraint("fk_access_assignment_operator_position", "access_role_assignments", type_="foreignkey")
    op.drop_constraint("fk_access_assignment_operator_period", "access_role_assignments", type_="foreignkey")
    op.drop_column("access_role_assignments", "operator_position_assignment_id")
    op.drop_column("access_role_assignments", "operator_employment_period_id")

    op.drop_index("uq_external_active_membership", table_name="external_organization_memberships")
    op.drop_index(
        "ix_external_organization_memberships_organization_id",
        table_name="external_organization_memberships",
    )
    op.drop_index(
        "ix_external_organization_memberships_user_id",
        table_name="external_organization_memberships",
    )
    op.drop_table("external_organization_memberships")

    op.drop_index("uq_operator_active_position", table_name="operator_position_assignments")
    op.drop_index(
        "ix_operator_position_assignments_employment_period_id",
        table_name="operator_position_assignments",
    )
    op.drop_table("operator_position_assignments")

    op.drop_index("uq_operator_personnel_number", table_name="operator_employment_periods")
    op.drop_index("uq_operator_active_period", table_name="operator_employment_periods")
    op.drop_index(
        "ix_operator_employment_periods_user_id",
        table_name="operator_employment_periods",
    )
    op.drop_table("operator_employment_periods")
    op.drop_table("operator_positions")

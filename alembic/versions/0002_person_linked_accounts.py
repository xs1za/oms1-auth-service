"""add person linked accounts and access roles

Revision ID: 0002_person_linked_accounts
Revises: 0001_create_auth_tables
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_person_linked_accounts"
down_revision: str | None = "0001_create_auth_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("person_id", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("display_name", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("name_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("auth_version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("users", sa.Column("merged_into_user_id", sa.String(length=36), nullable=True))
    op.create_index("ix_users_person_id", "users", ["person_id"])
    op.create_index(
        "uq_users_canonical_person",
        "users",
        ["person_id"],
        unique=True,
        postgresql_where=sa.text("person_id IS NOT NULL AND merged_into_user_id IS NULL AND is_active"),
    )

    op.create_table(
        "user_login_methods",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("method_type", sa.String(length=32), nullable=False),
        sa.Column("identifier", sa.String(length=255), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_login_methods_user_id", "user_login_methods", ["user_id"])
    op.create_index("ix_user_login_methods_identifier", "user_login_methods", ["identifier"])

    op.create_table(
        "access_role_assignments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role_code", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("granted_by", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("employment_episode_id", sa.Integer(), nullable=True),
        sa.Column("position_assignment_id", sa.Integer(), nullable=True),
        sa.Column("membership_id", sa.String(length=36), nullable=True),
        sa.Column("auth_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=36), nullable=True),
        sa.Column("revoke_reason", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_access_role_assignments_user_id", "access_role_assignments", ["user_id"])
    op.create_index("ix_access_role_assignments_role_code", "access_role_assignments", ["role_code"])

    op.create_table(
        "access_event_inbox",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("aggregate_id", sa.Integer(), nullable=False),
        sa.Column("aggregate_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("aggregate_id", "aggregate_version", name="uq_access_event_version"),
    )
    op.create_index("ix_access_event_inbox_aggregate_id", "access_event_inbox", ["aggregate_id"])

    op.create_table(
        "access_consumer_state",
        sa.Column("aggregate_id", sa.Integer(), nullable=False),
        sa.Column("last_version", sa.Integer(), nullable=False),
        sa.Column("last_event_id", sa.String(length=36), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("aggregate_id"),
    )


def downgrade() -> None:
    op.drop_table("access_consumer_state")
    op.drop_index("ix_access_event_inbox_aggregate_id", table_name="access_event_inbox")
    op.drop_table("access_event_inbox")
    op.drop_index("ix_access_role_assignments_role_code", table_name="access_role_assignments")
    op.drop_index("ix_access_role_assignments_user_id", table_name="access_role_assignments")
    op.drop_table("access_role_assignments")
    op.drop_index("ix_user_login_methods_identifier", table_name="user_login_methods")
    op.drop_index("ix_user_login_methods_user_id", table_name="user_login_methods")
    op.drop_table("user_login_methods")
    op.drop_index("uq_users_canonical_person", table_name="users")
    op.drop_index("ix_users_person_id", table_name="users")
    op.drop_column("users", "merged_into_user_id")
    op.drop_column("users", "auth_version")
    op.drop_column("users", "name_version")
    op.drop_column("users", "display_name")
    op.drop_column("users", "person_id")

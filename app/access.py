from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import (
    AccessRoleAssignment,
    ExternalOrganizationMembership,
    OperatorEmploymentPeriod,
    OperatorPositionAssignment,
    ServiceClient,
    User,
)

OPERATOR_ROLES = {"manager", "supervisor", "administrator"}
EXTERNAL_ROLE = "external_user"
ROLE_CODES = OPERATOR_ROLES | {EXTERNAL_ROLE}
EXTERNAL_SCOPES = ["shifts:publish", "assignment_stats:read"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def active_assignments(db: Session, user_id: str, at: datetime | None = None) -> list[AccessRoleAssignment]:
    moment = at or utcnow()
    return (
        db.query(AccessRoleAssignment)
        .filter(
            AccessRoleAssignment.user_id == user_id,
            AccessRoleAssignment.status == "active",
            AccessRoleAssignment.valid_from <= moment,
            (AccessRoleAssignment.valid_to.is_(None)) | (AccessRoleAssignment.valid_to > moment),
        )
        .order_by(AccessRoleAssignment.valid_from)
        .all()
    )


def roles_for(db: Session, user: User) -> list[str]:
    return sorted({assignment.role_code for assignment in active_assignments(db, user.id)})


def membership_active_at(membership: ExternalOrganizationMembership, at: datetime) -> bool:
    moment = as_aware(at)
    if membership.status != "active":
        return False
    if as_aware(membership.valid_from) > moment:
        return False
    return membership.valid_to is None or as_aware(membership.valid_to) > moment


def external_organization_id(db: Session, user: User) -> str | None:
    moment = utcnow()
    for assignment in active_assignments(db, user.id, moment):
        if assignment.role_code != EXTERNAL_ROLE or not assignment.membership_id:
            continue
        membership = (
            db.query(ExternalOrganizationMembership)
            .filter(ExternalOrganizationMembership.id == assignment.membership_id)
            .one_or_none()
        )
        if membership is None or membership.user_id != user.id:
            continue
        if membership_active_at(membership, moment):
            return membership.organization_id
    return None


def effective_scopes(db: Session, user: User) -> list[str]:
    scopes = list(user.scopes or [])
    if external_organization_id(db, user):
        for scope in EXTERNAL_SCOPES:
            if scope not in scopes:
                scopes.append(scope)
    return scopes


def bump_auth_version(user: User) -> None:
    user.auth_version = int(user.auth_version or 1) + 1
    user.updated_at = utcnow()


def decimal_id(value) -> str | None:
    return str(value) if value is not None else None


def parse_decimal(value, field: str):
    if value is None:
        return None
    if isinstance(value, bool):
        raise api_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            f"{field} must be a decimal BIGINT string",
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "validation_error", f"{field} must be a decimal BIGINT string") from None
    if parsed <= 0:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "validation_error", f"{field} must be positive")
    return parsed


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return as_aware(value).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def serialize_position_assignment(assignment: OperatorPositionAssignment) -> dict:
    return {
        "operator_position_assignment_id": assignment.id,
        "position_code": assignment.position_code,
        "status": assignment.status,
        "starts_at": iso(assignment.starts_at),
        "ends_at": iso(assignment.ends_at),
    }


def serialize_operator_employment_period(
    period: OperatorEmploymentPeriod, assignment: OperatorPositionAssignment | None
) -> dict:
    return {
        "operator_employment_period_id": period.id,
        "user_id": period.user_id,
        "status": period.status,
        "starts_at": iso(period.starts_at),
        "ends_at": iso(period.ends_at),
        "personnel_number": period.personnel_number,
        "position_assignment": serialize_position_assignment(assignment) if assignment is not None else None,
        "created_at": iso(period.created_at),
    }


def serialize_external_membership(membership: ExternalOrganizationMembership) -> dict:
    return {
        "membership_id": membership.id,
        "user_id": membership.user_id,
        "organization_id": membership.organization_id,
        "status": membership.status,
        "valid_from": iso(membership.valid_from),
        "valid_to": iso(membership.valid_to),
        "created_at": iso(membership.created_at),
    }


def serialize_assignment(
    assignment: AccessRoleAssignment, user: User | None = None, organization_id: str | None = None
) -> dict:
    return {
        "assignment_id": assignment.id,
        "user_id": assignment.user_id,
        "person_id": decimal_id(user.person_id) if user is not None else None,
        "role_code": assignment.role_code,
        "status": assignment.status,
        "valid_from": iso(assignment.valid_from),
        "valid_to": iso(assignment.valid_to),
        "operator_employment_period_id": assignment.operator_employment_period_id,
        "operator_position_assignment_id": assignment.operator_position_assignment_id,
        "organization_id": organization_id,
    }


def assignable_user(db: Session, user_id: str) -> User:
    user = db.query(User).filter(User.id == user_id).one_or_none()
    if user is None:
        raise api_error(status.HTTP_404_NOT_FOUND, "user_not_found", "User was not found")
    if user.merged_into_user_id is not None or not user.is_active:
        raise api_error(status.HTTP_409_CONFLICT, "person_account_conflict", "User is not an active canonical account")
    return user


def active_user(db: Session, user_id: str) -> User:
    user = db.query(User).filter(User.id == user_id).one_or_none()
    if user is None or user.merged_into_user_id is not None or not user.is_active:
        raise api_error(status.HTTP_404_NOT_FOUND, "user_not_found", "User was not found")
    return user


def service_client_by_subject(db: Session, subject: str) -> ServiceClient | None:
    return db.query(ServiceClient).filter(ServiceClient.client_id == subject).one_or_none()

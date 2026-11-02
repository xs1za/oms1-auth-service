from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import AccessRoleAssignment, ServiceClient, User

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


def external_organization_id(db: Session, user: User) -> str | None:
    for assignment in active_assignments(db, user.id):
        if assignment.role_code == EXTERNAL_ROLE:
            return assignment.organization_id
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
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "validation_error", f"{field} must be a decimal BIGINT string") from None
    if parsed <= 0:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "validation_error", f"{field} must be positive")
    return parsed


def serialize_assignment(assignment: AccessRoleAssignment, user: User | None = None) -> dict:
    return {
        "assignment_id": assignment.id,
        "user_id": assignment.user_id,
        "person_id": decimal_id(user.person_id) if user is not None else None,
        "role_code": assignment.role_code,
        "status": assignment.status,
        "valid_from": iso(assignment.valid_from),
        "valid_to": iso(assignment.valid_to),
        "employment_episode_id": decimal_id(assignment.employment_episode_id),
        "position_assignment_id": decimal_id(assignment.position_assignment_id),
        "organization_id": assignment.organization_id,
    }


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return as_aware(value).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def assignable_user(db: Session, user_id: str) -> User:
    user = db.query(User).filter(User.id == user_id).one_or_none()
    if user is None:
        raise api_error(status.HTTP_404_NOT_FOUND, "user_not_found", "User was not found")
    if user.merged_into_user_id is not None or not user.is_active:
        raise api_error(status.HTTP_409_CONFLICT, "person_account_conflict", "User is not an active canonical account")
    return user


def service_client_by_subject(db: Session, subject: str) -> ServiceClient | None:
    return db.query(ServiceClient).filter(ServiceClient.client_id == subject).one_or_none()

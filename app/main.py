from datetime import datetime, timedelta, timezone
import logging
import os
import re
from threading import Thread
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import (
    EXTERNAL_ROLE,
    ROLE_CODES,
    active_user,
    api_error,
    as_aware,
    assignable_user,
    bump_auth_version,
    decimal_id,
    effective_scopes,
    external_organization_id,
    iso,
    membership_active_at,
    parse_decimal,
    roles_for,
    serialize_assignment,
    serialize_external_membership,
    serialize_operator_employment_period,
    utcnow,
)
from app.access_consumer import run_access_consumer
from app.database import get_db
from app.kafka import publish_event
from app.healthcheck.router import router as healthcheck_router
from app.models import (
    AccessRoleAssignment,
    ExternalOrganizationMembership,
    OperatorEmploymentPeriod,
    OperatorPosition,
    OperatorPositionAssignment,
    ServiceClient,
    User,
    UserLoginMethod,
)
from app.settings import settings

logger = logging.getLogger(settings.service_name)

app = FastAPI(title="OMS1 Auth Service", version="0.3.0", root_path=settings.root_path)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8088", "http://127.0.0.1:8088"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(healthcheck_router)
security = HTTPBearer(auto_error=False)
password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class UserLogin(BaseModel):
    username: str
    password: str


class ServiceLogin(BaseModel):
    client_id: str
    client_secret: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


class RoleAssignmentCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    role_code: str
    operator_employment_period_id: str | None = None
    operator_position_assignment_id: str | None = None
    membership_id: str | None = None
    valid_from: datetime
    reason: str = Field(min_length=1, max_length=255)


class RoleAssignmentRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effective_at: datetime
    reason: str = Field(min_length=1, max_length=255)


class OperatorEmploymentPeriodCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starts_at: datetime
    position_code: str = Field(min_length=1, max_length=64)
    personnel_number: str | None = Field(default=None, max_length=250)
    reason: str = Field(min_length=1, max_length=255)


class OperatorEmploymentPeriodTerminate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effective_at: datetime
    reason: str = Field(min_length=1, max_length=255)


class ExternalMembershipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID
    valid_from: datetime
    valid_to: datetime | None = None


class ExternalMembershipRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effective_at: datetime
    reason: str = Field(min_length=1, max_length=255)


class PerformerLinkUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_id: str | None


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.info("Request validation failed", extra={"path": request.url.path})
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": {"code": "validation_error", "message": "Request validation failed"}},
    )


@app.on_event("startup")
def start_access_consumer() -> None:
    if os.environ.get("ENABLE_ACCESS_CONSUMER", "true").lower() not in {"0", "false", "no"}:
        Thread(target=run_access_consumer, daemon=True).start()


def create_token(subject: str, subject_type: str, scopes: list[str], extra: dict | None = None) -> TokenResponse:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": subject, "typ": subject_type, "scopes": scopes, "exp": expires_at}
    payload.update(extra or {})
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return TokenResponse(access_token=token, expires_at=expires_at)


def create_user_token(db: Session, user: User) -> TokenResponse:
    organization_id = external_organization_id(db, user)
    extra = {
        "uid": user.id,
        "roles": roles_for(db, user),
        "auth_version": int(user.auth_version or 1),
    }
    if user.person_id is not None:
        extra["person_id"] = str(user.person_id)
    if organization_id is not None:
        extra["organization_id"] = organization_id
    return create_token(user.username, "user", effective_scopes(db, user), extra)


def decode_token(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise api_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", "A bearer token is required")
    try:
        return jwt.decode(credentials.credentials, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise api_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", "Invalid token") from exc


def normalize_phone(raw: str) -> str | None:
    candidate = raw.strip()
    if not candidate.startswith(("+", "8", "7")) or sum(character.isdigit() for character in candidate) < 10:
        return None
    digits = re.sub(r"[^\d]", "", candidate)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) < 10 or len(digits) > 15:
        return None
    return "+" + digits


def find_login_user(db: Session, identifier: str) -> User | None:
    user = db.query(User).filter(User.username == identifier).one_or_none()
    if user is not None:
        return user
    phone = normalize_phone(identifier)
    if phone is None:
        return None
    now = utcnow()
    methods = (
        db.query(UserLoginMethod)
        .filter(
            UserLoginMethod.identifier == phone,
            UserLoginMethod.verified_at.isnot(None),
            (UserLoginMethod.valid_to.is_(None)) | (UserLoginMethod.valid_to > now),
        )
        .order_by(UserLoginMethod.verified_at.desc())
        .all()
    )
    for method in methods:
        candidate = db.query(User).filter(User.id == method.user_id).one_or_none()
        if candidate is not None and candidate.is_active and candidate.merged_into_user_id is None:
            return candidate
    return None


def resolve_token_user(db: Session, payload: dict) -> User | None:
    user_id = payload.get("uid")
    if user_id:
        return db.query(User).filter(User.id == user_id).one_or_none()
    subject = payload.get("sub")
    if not subject:
        return None
    return db.query(User).filter(User.username == subject).one_or_none()


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security), db: Session = Depends(get_db)
) -> User:
    payload = decode_token(credentials)
    if payload.get("typ") != "user":
        raise api_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", "A human user token is required")
    user = resolve_token_user(db, payload)
    if user is None or not user.is_active or user.merged_into_user_id is not None:
        raise api_error(status.HTTP_401_UNAUTHORIZED, "token_revoked", "Token is no longer valid")
    if int(payload.get("auth_version") or 1) != int(user.auth_version or 1):
        raise api_error(status.HTTP_401_UNAUTHORIZED, "token_revoked", "Access version has changed")
    current_roles = set(roles_for(db, user))
    if set(payload.get("roles") or []) - current_roles:
        raise api_error(status.HTTP_401_UNAUTHORIZED, "token_revoked", "A role in the token was revoked")
    return user


def current_administrator(user: User = Depends(current_user), db: Session = Depends(get_db)) -> User:
    if "administrator" not in roles_for(db, user):
        raise api_error(status.HTTP_403_FORBIDDEN, "administrator_required", "An administrator role is required")
    return user


def context_active(status_value: str, starts_at: datetime, ends_at: datetime | None, at: datetime) -> bool:
    moment = as_aware(at)
    return (
        status_value == "active"
        and as_aware(starts_at) <= moment
        and (ends_at is None or as_aware(ends_at) > moment)
    )


@app.post("/auth/token", response_model=TokenResponse)
def login(payload: UserLogin, db: Session = Depends(get_db)) -> TokenResponse:
    user = find_login_user(db, payload.username)
    if not user or not user.is_active or user.merged_into_user_id is not None:
        raise api_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", "Invalid credentials")
    if not password_context.verify(payload.password, user.password_hash):
        raise api_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", "Invalid credentials")
    token = create_user_token(db, user)
    publish_event("auth.user_logged_in", {"username": user.username, "at": utcnow()})
    return token


@app.post("/auth/service-token", response_model=TokenResponse)
def service_login(payload: ServiceLogin, db: Session = Depends(get_db)) -> TokenResponse:
    client = db.query(ServiceClient).filter(ServiceClient.client_id == payload.client_id).one_or_none()
    if not client or not client.is_active or not password_context.verify(payload.client_secret, client.secret_hash):
        raise api_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", "Invalid service credentials")
    token = create_token(payload.client_id, "service", client.scopes)
    publish_event("auth.service_token_issued", {"client_id": payload.client_id, "at": utcnow()})
    return token


@app.get("/auth/verify")
def verify_token(payload: dict = Depends(decode_token), db: Session = Depends(get_db)) -> dict:
    if payload.get("typ") == "service":
        return {
            "active": True,
            "subject": payload.get("sub"),
            "type": "service",
            "scopes": payload.get("scopes", []),
        }
    if payload.get("typ") != "user":
        raise api_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", "Unknown token type")

    user = resolve_token_user(db, payload)
    if user is None or not user.is_active or user.merged_into_user_id is not None:
        raise api_error(status.HTTP_401_UNAUTHORIZED, "token_revoked", "Token is no longer valid")
    if int(payload.get("auth_version") or 1) != int(user.auth_version or 1):
        raise api_error(status.HTTP_401_UNAUTHORIZED, "token_revoked", "Access version has changed")

    roles = roles_for(db, user)
    if set(payload.get("roles") or []) - set(roles):
        raise api_error(status.HTTP_401_UNAUTHORIZED, "token_revoked", "A role in the token was revoked")

    return {
        "active": True,
        "subject": payload.get("sub"),
        "type": "user",
        "scopes": payload.get("scopes", []),
        "user_id": user.id,
        "person_id": str(user.person_id) if user.person_id is not None else None,
        "display_name": user.display_name,
        "roles": roles,
        "organization_id": external_organization_id(db, user),
        "auth_version": int(user.auth_version or 1),
    }


@app.post(
    "/admin/users/{user_id}/operator-employment-periods",
    status_code=status.HTTP_201_CREATED,
)
def create_operator_employment_period(
    user_id: str,
    payload: OperatorEmploymentPeriodCreate,
    administrator: User = Depends(current_administrator),
    db: Session = Depends(get_db),
) -> dict:
    user = active_user(db, user_id)
    position = db.query(OperatorPosition).filter(OperatorPosition.code == payload.position_code).one_or_none()
    if position is None or not position.is_active:
        raise api_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_position_code",
            "operator position was not found or is inactive",
        )

    now = utcnow()
    starts_at = as_aware(payload.starts_at)
    if starts_at < now - timedelta(days=90) or starts_at > now + timedelta(days=90):
        raise api_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_period",
            "starts_at must be between 90 days in the past and 90 days in the future",
        )

    personnel_number = (payload.personnel_number or "").strip() or None
    if personnel_number is not None:
        duplicate = (
            db.query(OperatorEmploymentPeriod)
            .filter(OperatorEmploymentPeriod.personnel_number == personnel_number)
            .one_or_none()
        )
        if duplicate is not None:
            raise api_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "validation_error",
                "personnel_number must be unique",
            )

    active_period = (
        db.query(OperatorEmploymentPeriod)
        .filter(
            OperatorEmploymentPeriod.user_id == user.id,
            OperatorEmploymentPeriod.status == "active",
        )
        .one_or_none()
    )
    if active_period is not None:
        raise api_error(
            status.HTTP_409_CONFLICT,
            "operator_employment_already_active",
            "User already has an active operator employment period",
        )

    period = OperatorEmploymentPeriod(
        id=str(uuid4()),
        user_id=user.id,
        personnel_number=personnel_number,
        status="active",
        starts_at=starts_at,
        ends_at=None,
        termination_reason=None,
        source="hr",
        created_at=now,
        updated_at=now,
    )
    assignment = OperatorPositionAssignment(
        id=str(uuid4()),
        employment_period_id=period.id,
        position_code=position.code,
        status="active",
        starts_at=starts_at,
        ends_at=None,
        created_at=now,
        updated_at=now,
    )
    db.add(period)
    db.add(assignment)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(
            status.HTTP_409_CONFLICT,
            "operator_employment_already_active",
            "User already has an active operator employment period",
        ) from None
    db.refresh(period)
    db.refresh(assignment)

    logger.info(
        "Created operator employment period",
        extra={
            "user_id": user.id,
            "period_id": period.id,
            "assignment_id": assignment.id,
            "auth_version": int(user.auth_version or 1),
        },
    )
    return serialize_operator_employment_period(period, assignment)


@app.post("/admin/operator-employment-periods/{period_id}/terminate")
def terminate_operator_employment_period(
    period_id: str,
    payload: OperatorEmploymentPeriodTerminate,
    administrator: User = Depends(current_administrator),
    db: Session = Depends(get_db),
) -> dict:
    period = (
        db.query(OperatorEmploymentPeriod)
        .filter(OperatorEmploymentPeriod.id == period_id)
        .one_or_none()
    )
    if period is None:
        raise api_error(
            status.HTTP_404_NOT_FOUND,
            "operator_employment_period_not_found",
            "Operator employment period was not found",
        )

    effective_at = as_aware(payload.effective_at)
    if period.status == "terminated":
        same_effective_at = period.ends_at is not None and as_aware(period.ends_at) == effective_at
        same_reason = (period.termination_reason or "") == payload.reason
        if same_effective_at and same_reason:
            return {
                "operator_employment_period_id": period.id,
                "status": period.status,
                "ends_at": iso(period.ends_at),
                "revoked_role_assignments": 0,
            }
        raise api_error(
            status.HTTP_409_CONFLICT,
            "operator_employment_period_conflict",
            "Operator employment period is already terminated with different values",
        )

    if effective_at <= as_aware(period.starts_at):
        raise api_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_period",
            "effective_at must be later than starts_at",
        )

    now = utcnow()
    period.status = "terminated"
    period.ends_at = effective_at
    period.termination_reason = payload.reason
    period.updated_at = now

    position_assignments = (
        db.query(OperatorPositionAssignment)
        .filter(
            OperatorPositionAssignment.employment_period_id == period.id,
            OperatorPositionAssignment.status == "active",
        )
        .all()
    )
    for assignment in position_assignments:
        assignment.status = "terminated"
        assignment.ends_at = effective_at
        assignment.updated_at = now

    revoked_assignments = (
        db.query(AccessRoleAssignment)
        .filter(
            AccessRoleAssignment.operator_employment_period_id == period.id,
            AccessRoleAssignment.status == "active",
        )
        .all()
    )
    for assignment in revoked_assignments:
        assignment.status = "revoked"
        assignment.valid_to = effective_at
        assignment.revoked_at = now
        assignment.revoked_by = administrator.id
        assignment.revoke_reason = payload.reason
        assignment.auth_version = int(assignment.auth_version or 1) + 1

    owner = db.query(User).filter(User.id == period.user_id).one_or_none()
    if owner is not None:
        bump_auth_version(owner)

    db.commit()
    db.refresh(period)

    logger.info(
        "Terminated operator employment period",
        extra={
            "user_id": period.user_id,
            "period_id": period.id,
            "revoked_role_assignments": len(revoked_assignments),
            "auth_version": int(owner.auth_version or 1) if owner is not None else None,
        },
    )
    return {
        "operator_employment_period_id": period.id,
        "status": period.status,
        "ends_at": iso(period.ends_at),
        "revoked_role_assignments": len(revoked_assignments),
    }


@app.post(
    "/admin/users/{user_id}/external-memberships",
    status_code=status.HTTP_201_CREATED,
)
def create_external_membership(
    user_id: str,
    payload: ExternalMembershipCreate,
    administrator: User = Depends(current_administrator),
    db: Session = Depends(get_db),
) -> dict:
    user = active_user(db, user_id)
    organization_id = str(payload.organization_id)
    valid_from = as_aware(payload.valid_from)
    valid_to = as_aware(payload.valid_to) if payload.valid_to is not None else None
    if valid_to is not None and valid_to <= valid_from:
        raise api_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_period",
            "valid_to must be later than valid_from",
        )

    existing = (
        db.query(ExternalOrganizationMembership)
        .filter(
            ExternalOrganizationMembership.user_id == user.id,
            ExternalOrganizationMembership.organization_id == organization_id,
            ExternalOrganizationMembership.status == "active",
        )
        .one_or_none()
    )
    if existing is not None:
        raise api_error(
            status.HTTP_409_CONFLICT,
            "external_membership_already_active",
            "User already has an active membership in this organization",
        )

    now = utcnow()
    membership = ExternalOrganizationMembership(
        id=str(uuid4()),
        user_id=user.id,
        organization_id=organization_id,
        status="active",
        valid_from=valid_from,
        valid_to=valid_to,
        revoked_reason=None,
        created_at=now,
        revoked_at=None,
    )
    db.add(membership)
    bump_auth_version(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(
            status.HTTP_409_CONFLICT,
            "external_membership_already_active",
            "User already has an active membership in this organization",
        ) from None
    db.refresh(membership)

    logger.info(
        "Created external organization membership",
        extra={
            "user_id": user.id,
            "membership_id": membership.id,
            "organization_id": membership.organization_id,
            "auth_version": int(user.auth_version or 1),
        },
    )
    return serialize_external_membership(membership)


@app.post("/admin/external-memberships/{membership_id}/revoke")
def revoke_external_membership(
    membership_id: str,
    payload: ExternalMembershipRevoke,
    administrator: User = Depends(current_administrator),
    db: Session = Depends(get_db),
) -> dict:
    membership = (
        db.query(ExternalOrganizationMembership)
        .filter(ExternalOrganizationMembership.id == membership_id)
        .one_or_none()
    )
    if membership is None:
        raise api_error(
            status.HTTP_404_NOT_FOUND,
            "external_membership_not_found",
            "External organization membership was not found",
        )

    effective_at = as_aware(payload.effective_at)
    if membership.status == "revoked":
        same_effective_at = membership.valid_to is not None and as_aware(membership.valid_to) == effective_at
        same_reason = (membership.revoked_reason or "") == payload.reason
        if same_effective_at and same_reason:
            return {
                "membership_id": membership.id,
                "status": membership.status,
                "valid_to": iso(membership.valid_to),
                "revoked_role_assignments": 0,
            }
        raise api_error(
            status.HTTP_409_CONFLICT,
            "external_membership_conflict",
            "External membership is already revoked with different values",
        )

    if effective_at <= as_aware(membership.valid_from):
        raise api_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_period",
            "effective_at must be later than valid_from",
        )

    now = utcnow()
    membership.status = "revoked"
    membership.valid_to = effective_at
    membership.revoked_reason = payload.reason
    membership.revoked_at = now

    revoked_assignments = (
        db.query(AccessRoleAssignment)
        .filter(
            AccessRoleAssignment.membership_id == membership.id,
            AccessRoleAssignment.role_code == EXTERNAL_ROLE,
            AccessRoleAssignment.status == "active",
        )
        .all()
    )
    for assignment in revoked_assignments:
        assignment.status = "revoked"
        assignment.valid_to = effective_at
        assignment.revoked_at = now
        assignment.revoked_by = administrator.id
        assignment.revoke_reason = payload.reason
        assignment.auth_version = int(assignment.auth_version or 1) + 1

    owner = db.query(User).filter(User.id == membership.user_id).one_or_none()
    if owner is not None:
        bump_auth_version(owner)

    db.commit()
    db.refresh(membership)

    logger.info(
        "Revoked external organization membership",
        extra={
            "user_id": membership.user_id,
            "membership_id": membership.id,
            "revoked_role_assignments": len(revoked_assignments),
            "auth_version": int(owner.auth_version or 1) if owner is not None else None,
        },
    )
    return {
        "membership_id": membership.id,
        "status": membership.status,
        "valid_to": iso(membership.valid_to),
        "revoked_role_assignments": len(revoked_assignments),
    }


@app.post("/admin/users/{user_id}/role-assignments", status_code=status.HTTP_201_CREATED)
def create_role_assignment(
    user_id: str,
    payload: RoleAssignmentCreate,
    administrator: User = Depends(current_administrator),
    db: Session = Depends(get_db),
) -> dict:
    user = assignable_user(db, user_id)

    legacy_fields = {"employment_episode_id", "position_assignment_id", "organization_id"}
    extra_fields = set(payload.model_extra or {})
    if extra_fields:
        code = "invalid_role_context" if extra_fields & legacy_fields else "validation_error"
        raise api_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            code,
            "OMS2 employment context and organization_id are not accepted",
        )

    if payload.role_code not in ROLE_CODES:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_role_context", "Unknown role_code")

    valid_from = as_aware(payload.valid_from)
    organization_id: str | None = None
    membership_id: str | None = None
    operator_period_id: str | None = None
    operator_assignment_id: str | None = None

    if payload.role_code == EXTERNAL_ROLE:
        if (
            payload.operator_employment_period_id is not None
            or payload.operator_position_assignment_id is not None
        ):
            raise api_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_role_context",
                "external_user must not reference operator employment context",
            )
        if not payload.membership_id:
            raise api_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_role_context",
                "external_user requires membership_id",
            )
        membership = (
            db.query(ExternalOrganizationMembership)
            .filter(ExternalOrganizationMembership.id == payload.membership_id)
            .one_or_none()
        )
        if membership is None or membership.user_id != user.id:
            raise api_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_role_context",
                "External membership was not found for this user",
            )
        if not membership_active_at(membership, valid_from):
            raise api_error(
                status.HTTP_409_CONFLICT,
                "role_context_not_active",
                "External membership is not active at valid_from",
            )
        membership_id = membership.id
        organization_id = membership.organization_id
    else:
        if payload.membership_id is not None:
            raise api_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_role_context",
                "Operator roles must not reference external membership context",
            )
        if not payload.operator_employment_period_id or not payload.operator_position_assignment_id:
            raise api_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_role_context",
                "Operator roles require operator_employment_period_id and operator_position_assignment_id",
            )
        period = (
            db.query(OperatorEmploymentPeriod)
            .filter(OperatorEmploymentPeriod.id == payload.operator_employment_period_id)
            .one_or_none()
        )
        if period is None or period.user_id != user.id:
            raise api_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_role_context",
                "Operator employment period was not found for this user",
            )
        position_assignment = (
            db.query(OperatorPositionAssignment)
            .filter(OperatorPositionAssignment.id == payload.operator_position_assignment_id)
            .one_or_none()
        )
        if position_assignment is None or position_assignment.employment_period_id != period.id:
            raise api_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_role_context",
                "Operator position assignment does not belong to the specified period",
            )
        if not context_active(period.status, period.starts_at, period.ends_at, valid_from):
            raise api_error(
                status.HTTP_409_CONFLICT,
                "role_context_not_active",
                "Operator employment period is not active at valid_from",
            )
        if not context_active(
            position_assignment.status,
            position_assignment.starts_at,
            position_assignment.ends_at,
            valid_from,
        ):
            raise api_error(
                status.HTTP_409_CONFLICT,
                "role_context_not_active",
                "Operator position assignment is not active at valid_from",
            )
        operator_period_id = period.id
        operator_assignment_id = position_assignment.id

    duplicate_query = db.query(AccessRoleAssignment).filter(
        AccessRoleAssignment.user_id == user.id,
        AccessRoleAssignment.role_code == payload.role_code,
        AccessRoleAssignment.status == "active",
    )
    if payload.role_code == EXTERNAL_ROLE:
        duplicate_query = duplicate_query.filter(AccessRoleAssignment.membership_id == membership_id)
    else:
        duplicate_query = duplicate_query.filter(
            AccessRoleAssignment.operator_employment_period_id == operator_period_id,
            AccessRoleAssignment.operator_position_assignment_id == operator_assignment_id,
        )
    if duplicate_query.first() is not None:
        raise api_error(
            status.HTTP_409_CONFLICT,
            "role_assignment_already_active",
            "An active assignment already exists for this role context",
        )

    now = utcnow()
    assignment = AccessRoleAssignment(
        id=str(uuid4()),
        user_id=user.id,
        role_code=payload.role_code,
        valid_from=valid_from,
        valid_to=None,
        status="active",
        granted_by=administrator.id,
        reason=payload.reason,
        operator_employment_period_id=operator_period_id,
        operator_position_assignment_id=operator_assignment_id,
        membership_id=membership_id,
        granted_at=now,
    )
    db.add(assignment)
    bump_auth_version(user)
    db.commit()
    db.refresh(assignment)

    logger.info(
        "Created access role assignment",
        extra={
            "user_id": user.id,
            "assignment_id": assignment.id,
            "role_code": assignment.role_code,
            "period_id": assignment.operator_employment_period_id,
            "membership_id": assignment.membership_id,
            "auth_version": int(user.auth_version or 1),
        },
    )
    return serialize_assignment(assignment, user, organization_id=organization_id)


@app.post("/admin/role-assignments/{assignment_id}/revoke")
def revoke_role_assignment(
    assignment_id: str,
    payload: RoleAssignmentRevoke,
    administrator: User = Depends(current_administrator),
    db: Session = Depends(get_db),
) -> dict:
    assignment = db.query(AccessRoleAssignment).filter(AccessRoleAssignment.id == assignment_id).one_or_none()
    if assignment is None:
        raise api_error(status.HTTP_404_NOT_FOUND, "role_assignment_not_found", "Role assignment was not found")
    if assignment.status != "active":
        return {
            "assignment_id": assignment.id,
            "status": assignment.status,
            "valid_to": iso(assignment.valid_to),
            "revoked_by_user_id": assignment.revoked_by,
            "reason": assignment.revoke_reason or assignment.reason,
        }

    effective_at = as_aware(payload.effective_at)
    if assignment.role_code == "administrator":
        remaining = (
            db.query(AccessRoleAssignment)
            .filter(
                AccessRoleAssignment.role_code == "administrator",
                AccessRoleAssignment.status == "active",
                AccessRoleAssignment.id != assignment.id,
            )
            .count()
        )
        if remaining == 0:
            raise api_error(
                status.HTTP_409_CONFLICT,
                "last_administrator",
                "At least one active administrator must remain",
            )

    assignment.status = "revoked"
    assignment.valid_to = effective_at
    assignment.revoked_at = utcnow()
    assignment.revoked_by = administrator.id
    assignment.revoke_reason = payload.reason
    assignment.auth_version = int(assignment.auth_version or 1) + 1

    owner = db.query(User).filter(User.id == assignment.user_id).one_or_none()
    if owner is not None:
        bump_auth_version(owner)
    db.commit()
    db.refresh(assignment)

    logger.info(
        "Revoked access role assignment",
        extra={
            "user_id": assignment.user_id,
            "assignment_id": assignment.id,
            "auth_version": int(owner.auth_version or 1) if owner is not None else None,
        },
    )
    return {
        "assignment_id": assignment.id,
        "status": assignment.status,
        "valid_to": iso(assignment.valid_to),
        "revoked_by_user_id": assignment.revoked_by,
        "reason": assignment.revoke_reason,
    }


@app.put("/admin/users/{user_id}/performer-link")
def set_performer_link(
    user_id: str,
    payload: PerformerLinkUpdate,
    administrator: User = Depends(current_administrator),
    db: Session = Depends(get_db),
) -> dict:
    user = active_user(db, user_id)
    person_id = parse_decimal(payload.person_id, "person_id")

    if person_id == user.person_id:
        return {"user_id": user.id, "person_id": decimal_id(user.person_id)}

    if person_id is not None:
        conflict = (
            db.query(User)
            .filter(
                User.person_id == person_id,
                User.id != user.id,
                User.merged_into_user_id.is_(None),
            )
            .one_or_none()
        )
        if conflict is not None:
            raise api_error(
                status.HTTP_409_CONFLICT,
                "person_account_conflict",
                "Another active account is already linked to this person_id",
            )

    user.person_id = person_id
    bump_auth_version(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(
            status.HTTP_409_CONFLICT,
            "person_account_conflict",
            "Another active account is already linked to this person_id",
        ) from None

    logger.info(
        "Updated user performer link",
        extra={
            "user_id": user.id,
            "person_id": decimal_id(user.person_id),
            "auth_version": int(user.auth_version or 1),
        },
    )
    return {"user_id": user.id, "person_id": decimal_id(user.person_id)}

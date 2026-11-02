from datetime import datetime, timedelta, timezone
import os
import re
from threading import Thread
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.access import (
    EXTERNAL_ROLE,
    OPERATOR_ROLES,
    ROLE_CODES,
    active_assignments,
    api_error,
    assignable_user,
    bump_auth_version,
    effective_scopes,
    external_organization_id,
    iso,
    parse_decimal,
    roles_for,
    serialize_assignment,
    utcnow,
)
from app.access_consumer import run_access_consumer
from app.database import get_db
from app.kafka import publish_event
from app.healthcheck.router import router as healthcheck_router
from app.models import AccessRoleAssignment, ServiceClient, User, UserLoginMethod
from app.settings import settings

app = FastAPI(title="OMS1 Auth Service", version="0.2.0", root_path=settings.root_path)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8088", "http://127.0.0.1:8088"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(healthcheck_router)
security = HTTPBearer()
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
    role_code: str
    employment_episode_id: str | int | None = None
    position_assignment_id: str | int | None = None
    membership_id: str | None = None
    organization_id: str | None = None
    valid_from: datetime
    reason: str = Field(min_length=1, max_length=255)


class RoleAssignmentRevoke(BaseModel):
    effective_at: datetime
    reason: str = Field(min_length=1, max_length=255)


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


def decode_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    try:
        return jwt.decode(credentials.credentials, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc


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
    credentials: HTTPAuthorizationCredentials = Depends(security), db: Session = Depends(get_db)
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


@app.post("/auth/token", response_model=TokenResponse)
def login(payload: UserLogin, db: Session = Depends(get_db)) -> TokenResponse:
    user = find_login_user(db, payload.username)
    if not user or not user.is_active or user.merged_into_user_id is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not password_context.verify(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token = create_user_token(db, user)
    publish_event("auth.user_logged_in", {"username": user.username, "at": utcnow()})
    return token


@app.post("/auth/service-token", response_model=TokenResponse)
def service_login(payload: ServiceLogin, db: Session = Depends(get_db)) -> TokenResponse:
    client = db.query(ServiceClient).filter(ServiceClient.client_id == payload.client_id).one_or_none()
    if not client or not client.is_active or not password_context.verify(payload.client_secret, client.secret_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid service credentials")
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


@app.post("/admin/users/{user_id}/role-assignments", status_code=status.HTTP_201_CREATED)
def create_role_assignment(
    user_id: str,
    payload: RoleAssignmentCreate,
    administrator: User = Depends(current_administrator),
    db: Session = Depends(get_db),
) -> dict:
    user = assignable_user(db, user_id)
    if payload.role_code not in ROLE_CODES:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_role_code", "Unknown role_code")

    if payload.role_code == EXTERNAL_ROLE:
        if not payload.membership_id or not payload.organization_id:
            raise api_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_role_context",
                "external_user requires membership_id and organization_id",
            )
        if payload.employment_episode_id is not None or payload.position_assignment_id is not None:
            raise api_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_role_context",
                "external_user must not reference employment context",
            )
        employment_episode_id = None
        position_assignment_id = None
    else:
        employment_episode_id = parse_decimal(payload.employment_episode_id, "employment_episode_id")
        position_assignment_id = parse_decimal(payload.position_assignment_id, "position_assignment_id")
        if employment_episode_id is None or position_assignment_id is None:
            raise api_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_role_context",
                "operator roles require employment_episode_id and position_assignment_id",
            )
        if payload.membership_id or payload.organization_id:
            raise api_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_role_context",
                "operator roles must not reference external membership context",
            )

    existing = active_assignments(db, user.id)
    for assignment in existing:
        if assignment.role_code != payload.role_code:
            continue
        if payload.role_code == EXTERNAL_ROLE:
            if assignment.organization_id == payload.organization_id:
                raise api_error(
                    status.HTTP_409_CONFLICT,
                    "role_assignment_already_active",
                    "An active assignment already exists for this organization",
                )
        elif (
            assignment.employment_episode_id == employment_episode_id
            and assignment.position_assignment_id == position_assignment_id
        ):
            raise api_error(
                status.HTTP_409_CONFLICT,
                "role_assignment_already_active",
                "An active assignment already exists for this employment context",
            )

    now = utcnow()
    valid_from = payload.valid_from if payload.valid_from.tzinfo else payload.valid_from.replace(tzinfo=timezone.utc)
    assignment = AccessRoleAssignment(
        id=str(uuid4()),
        user_id=user.id,
        role_code=payload.role_code,
        organization_id=payload.organization_id if payload.role_code == EXTERNAL_ROLE else None,
        valid_from=valid_from,
        valid_to=None,
        status="active",
        granted_by=administrator.id,
        reason=payload.reason,
        employment_episode_id=employment_episode_id,
        position_assignment_id=position_assignment_id,
        membership_id=payload.membership_id if payload.role_code == EXTERNAL_ROLE else None,
        granted_at=now,
    )
    db.add(assignment)
    bump_auth_version(user)
    db.commit()
    db.refresh(assignment)
    return serialize_assignment(assignment, user)


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

    effective_at = payload.effective_at if payload.effective_at.tzinfo else payload.effective_at.replace(tzinfo=timezone.utc)
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

    return {
        "assignment_id": assignment.id,
        "status": assignment.status,
        "valid_to": iso(assignment.valid_to),
        "revoked_by_user_id": assignment.revoked_by,
        "reason": assignment.revoke_reason,
    }

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.access import as_aware, bump_auth_version
from app.database import SessionLocal
from app.main import password_context
from app.models import (
    AccessRoleAssignment,
    OperatorEmploymentPeriod,
    OperatorPosition,
    OperatorPositionAssignment,
    ServiceClient,
    User,
)
from app.settings import settings


logger = logging.getLogger(__name__)

ADMIN_SCOPES = ["users", "services", "employees", "reports", "notifications", "operations"]
ADMIN_PERSONNEL_NUMBER = "ADMIN-001"
BOOTSTRAP_POSITION_CODE = "operator_software"
BOOTSTRAP_POSITION_NAME = "Оператор ПО"
SERVICE_CLIENTS = {
    "OMS2": (settings.oms2_service_secret, ["auth.verify", "employees"]),
    "OMS3": (settings.oms3_service_secret, ["auth.verify", "reports"]),
    "OMS4": (settings.oms4_service_secret, ["auth.verify", "notifications"]),
    "OMS5": (settings.oms5_service_secret, ["auth.verify", "operations"]),
}


def require_secret(value: str | None, name: str) -> str:
    if not value:
        raise RuntimeError(f"Required seed secret is missing: {name}")
    return value


def ensure_operator_position(db: Session) -> bool:
    existing = db.query(OperatorPosition).filter(OperatorPosition.code == BOOTSTRAP_POSITION_CODE).one_or_none()
    if existing is not None:
        return False
    db.add(
        OperatorPosition(
            code=BOOTSTRAP_POSITION_CODE,
            name=BOOTSTRAP_POSITION_NAME,
            is_active=True,
        )
    )
    db.flush()
    return True


def ensure_admin_employment(db: Session, admin: User, now: datetime) -> tuple[OperatorEmploymentPeriod, OperatorPositionAssignment]:
    period = (
        db.query(OperatorEmploymentPeriod)
        .filter(
            OperatorEmploymentPeriod.user_id == admin.id,
            OperatorEmploymentPeriod.personnel_number == ADMIN_PERSONNEL_NUMBER,
        )
        .one_or_none()
    )
    if period is None:
        active_period = (
            db.query(OperatorEmploymentPeriod)
            .filter(
                OperatorEmploymentPeriod.user_id == admin.id,
                OperatorEmploymentPeriod.status == "active",
            )
            .one_or_none()
        )
        period = active_period
    if period is None:
        period = OperatorEmploymentPeriod(
            id=str(uuid4()),
            user_id=admin.id,
            personnel_number=ADMIN_PERSONNEL_NUMBER,
            status="active",
            starts_at=now - timedelta(days=1),
            ends_at=None,
            termination_reason=None,
            source="hr",
            created_at=now,
            updated_at=now,
        )
        db.add(period)
        db.flush()

    assignment = (
        db.query(OperatorPositionAssignment)
        .filter(
            OperatorPositionAssignment.employment_period_id == period.id,
            OperatorPositionAssignment.status == "active",
        )
        .one_or_none()
    )
    if assignment is None:
        assignment = OperatorPositionAssignment(
            id=str(uuid4()),
            employment_period_id=period.id,
            position_code=BOOTSTRAP_POSITION_CODE,
            status="active",
            starts_at=period.starts_at,
            ends_at=None,
            created_at=now,
            updated_at=now,
        )
        db.add(assignment)
        db.flush()
    return period, assignment


def seed_auth_data(db: Session) -> dict[str, int]:
    created_users = 0
    created_service_clients = 0
    created_assignments = 0
    now = datetime.now(timezone.utc)

    ensure_operator_position(db)

    admin = db.query(User).filter(User.username == "admin").one_or_none()
    if admin is None:
        admin_password = require_secret(settings.admin_initial_password, "ADMIN_INITIAL_PASSWORD")
        admin = User(
            id=str(uuid4()),
            username="admin",
            password_hash=password_context.hash(admin_password),
            scopes=ADMIN_SCOPES,
            display_name="Administrator",
        )
        db.add(admin)
        db.flush()
        created_users += 1

    period, position_assignment = ensure_admin_employment(db, admin, now)

    active_admin_assignment = (
        db.query(AccessRoleAssignment)
        .filter(
            AccessRoleAssignment.user_id == admin.id,
            AccessRoleAssignment.role_code == "administrator",
            AccessRoleAssignment.status == "active",
        )
        .one_or_none()
    )
    if active_admin_assignment is None:
        db.add(
            AccessRoleAssignment(
                id=str(uuid4()),
                user_id=admin.id,
                role_code="administrator",
                valid_from=now,
                valid_to=None,
                status="active",
                reason="Bootstrap administrator assignment",
                operator_employment_period_id=period.id,
                operator_position_assignment_id=position_assignment.id,
            )
        )
        created_assignments += 1
    else:
        changed = False
        if active_admin_assignment.operator_employment_period_id != period.id:
            active_admin_assignment.operator_employment_period_id = period.id
            changed = True
        if active_admin_assignment.operator_position_assignment_id != position_assignment.id:
            active_admin_assignment.operator_position_assignment_id = position_assignment.id
            changed = True
        if as_aware(active_admin_assignment.valid_from) < as_aware(period.starts_at):
            active_admin_assignment.valid_from = period.starts_at
            changed = True
        if changed:
            bump_auth_version(admin)

    for client_id, (secret, scopes) in SERVICE_CLIENTS.items():
        if db.query(ServiceClient).filter(ServiceClient.client_id == client_id).one_or_none() is not None:
            continue
        service_secret = require_secret(secret, f"{client_id}_SERVICE_SECRET")
        db.add(
            ServiceClient(
                id=str(uuid4()),
                client_id=client_id,
                service_name=client_id,
                secret_hash=password_context.hash(service_secret),
                scopes=scopes,
            )
        )
        created_service_clients += 1

    db.commit()
    return {"users": created_users, "service_clients": created_service_clients, "assignments": created_assignments}


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    with SessionLocal() as db:
        result = seed_auth_data(db)
    logger.info(
        "Seeded OMS1 auth data: users=%s service_clients=%s assignments=%s",
        result["users"],
        result["service_clients"],
        result["assignments"],
    )


if __name__ == "__main__":
    main()

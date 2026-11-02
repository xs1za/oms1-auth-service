import logging
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.main import password_context
from app.models import AccessRoleAssignment, ServiceClient, User
from app.settings import settings


logger = logging.getLogger(__name__)

ADMIN_SCOPES = ["users", "services", "employees", "reports", "notifications", "operations"]
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


def seed_auth_data(db: Session) -> dict[str, int]:
    created_users = 0
    created_service_clients = 0
    created_assignments = 0

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
                valid_from=datetime.now(timezone.utc),
                status="active",
                reason="Bootstrap administrator assignment",
            )
        )
        created_assignments += 1

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

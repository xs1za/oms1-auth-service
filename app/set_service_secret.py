import argparse
import logging

from app.database import SessionLocal
from app.main import password_context
from app.models import ServiceClient


logger = logging.getLogger(__name__)


def set_service_secret(client_id: str, secret: str) -> None:
    with SessionLocal() as db:
        client = db.query(ServiceClient).filter(ServiceClient.client_id == client_id).one_or_none()
        if client is None:
            raise RuntimeError(f"Service client not found: {client_id}")
        client.secret_hash = password_context.hash(secret)
        db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Update OMS1 service client secret hash")
    parser.add_argument("client_id")
    parser.add_argument("secret")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    set_service_secret(args.client_id, args.secret)
    logger.info("Updated secret hash for service client %s", args.client_id)


if __name__ == "__main__":
    main()

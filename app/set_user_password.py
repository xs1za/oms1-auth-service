import argparse
import logging

from app.database import SessionLocal
from app.main import password_context
from app.models import User


logger = logging.getLogger(__name__)


def set_user_password(username: str, password: str) -> None:
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).one_or_none()
        if user is None:
            raise RuntimeError(f"User not found: {username}")
        user.password_hash = password_context.hash(password)
        db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Update OMS1 user password hash")
    parser.add_argument("username")
    parser.add_argument("password")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    set_user_password(args.username, args.password)
    logger.info("Updated password hash for user %s", args.username)


if __name__ == "__main__":
    main()

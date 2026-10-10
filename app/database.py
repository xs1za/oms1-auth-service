from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.settings import settings


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_database() -> dict:
    try:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}

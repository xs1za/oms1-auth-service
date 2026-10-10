import socket
import time
from urllib.parse import urlparse

from app.settings import settings


def main() -> None:
    parsed = urlparse(settings.database_url)
    host = parsed.hostname
    port = parsed.port or 5432
    if not host:
        raise SystemExit("Database host is not configured")

    for _ in range(60):
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except OSError:
            print(f"Waiting for PostgreSQL at {host}:{port}...", flush=True)
            time.sleep(2)
    raise SystemExit("PostgreSQL is not available")


if __name__ == "__main__":
    main()

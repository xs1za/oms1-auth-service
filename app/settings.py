from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    service_name: str = "OMS1"
    root_path: str = ""
    jwt_secret_key: str = "change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    kafka_bootstrap_servers: str = "kafka.oms.svc.cluster.local:9092"
    database_url: str = "postgresql+psycopg://oms1@oms1-postgres.oms.svc.cluster.local:5432/oms1"
    admin_initial_password: str | None = None
    oms2_service_secret: str | None = None
    oms3_service_secret: str | None = None
    oms4_service_secret: str | None = None
    oms5_service_secret: str | None = None


settings = Settings()

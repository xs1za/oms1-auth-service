from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    service_name: str = "OMS1"
    root_path: str = ""
    jwt_secret_key: str = "change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    kafka_bootstrap_servers: str = "kafka.oms.svc.cluster.local:9092"


settings = Settings()

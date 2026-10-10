from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.kafka import publish_event
from app.healthcheck.router import router as healthcheck_router
from app.models import ServiceClient, User
from app.settings import settings

app = FastAPI(title="OMS1 Auth Service", version="0.1.0", root_path=settings.root_path)
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


def create_token(subject: str, subject_type: str, scopes: list[str]) -> TokenResponse:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": subject, "typ": subject_type, "scopes": scopes, "exp": expires_at}
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return TokenResponse(access_token=token, expires_at=expires_at)


def decode_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    try:
        return jwt.decode(credentials.credentials, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc


@app.post("/auth/token", response_model=TokenResponse)
def login(payload: UserLogin, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.query(User).filter(User.username == payload.username).one_or_none()
    if not user or not user.is_active or not password_context.verify(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token = create_token(payload.username, "user", user.scopes)
    publish_event("auth.user_logged_in", {"username": payload.username, "at": datetime.now(timezone.utc)})
    return token


@app.post("/auth/service-token", response_model=TokenResponse)
def service_login(payload: ServiceLogin, db: Session = Depends(get_db)) -> TokenResponse:
    client = db.query(ServiceClient).filter(ServiceClient.client_id == payload.client_id).one_or_none()
    if not client or not client.is_active or not password_context.verify(payload.client_secret, client.secret_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid service credentials")
    token = create_token(payload.client_id, "service", client.scopes)
    publish_event("auth.service_token_issued", {"client_id": payload.client_id, "at": datetime.now(timezone.utc)})
    return token


@app.get("/auth/verify")
def verify_token(payload: dict = Depends(decode_token)) -> dict:
    return {"active": True, "subject": payload.get("sub"), "type": payload.get("typ"), "scopes": payload.get("scopes", [])}

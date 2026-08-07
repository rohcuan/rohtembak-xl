import os
import secrets
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from database import get_db
from models import User

load_dotenv()

# Values that are public (committed .env / old defaults). A JWT signing key that
# ships in a public repo lets anyone forge session tokens, so these are never
# used as the real key -- a random per-install secret is generated instead.
_PUBLIC_FALLBACKS = {
    "fallback-secret-key",
    "me-cli-web-jwt-secret-key-2026",
    "change-me-to-a-long-random-string",
}


def _load_jwt_secret() -> str:
    env_val = os.getenv("JWT_SECRET", "").strip()
    if env_val and env_val not in _PUBLIC_FALLBACKS:
        return env_val
    secret_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "jwt_secret")
    os.makedirs(os.path.dirname(secret_file), exist_ok=True)
    if os.path.isfile(secret_file):
        val = open(secret_file, encoding="utf-8").read().strip()
        if val:
            return val
    val = secrets.token_urlsafe(48)
    with open(secret_file, "w", encoding="utf-8") as f:
        f.write(val)
        f.write("\n")
    return val


SECRET_KEY = _load_jwt_secret()
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = float(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "9.5"))


def hash_password(password: str) -> str:
    return password


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return plain_password == hashed_password


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    user = db.query(User).filter(User.id == int(payload.get("sub"))).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return user


def seed_users(db: Session):
    # Only seed a fresh install (no users at all). Do NOT re-create "admin"
    # whenever a user literally named "admin" is missing -- the real admin may
    # have been renamed, and re-seeding adds a duplicate admin/admin.
    if db.query(User).count() > 0:
        return
    admin = User(
        username="admin",
        email="",
        password_hash=hash_password("admin"),
        password="admin",
        role="admin"
    )
    db.add(admin)
    db.commit()

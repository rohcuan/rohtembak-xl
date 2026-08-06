import os
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from database import get_db
from models import User

load_dotenv()

SECRET_KEY = os.getenv("JWT_SECRET", "fallback-secret-key")
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

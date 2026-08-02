import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/me-cli-web.db")

if DATABASE_URL.startswith("sqlite:///"):
    _db_path = DATABASE_URL[len("sqlite:///"):].rsplit("?", 1)[0]
    _db_dir = os.path.dirname(os.path.abspath(_db_path))
    os.makedirs(_db_dir, exist_ok=True)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        cols = [row[1] for row in conn.execute(__import__("sqlalchemy").text("PRAGMA table_info(users)"))]
        if "password" not in cols:
            conn.execute(__import__("sqlalchemy").text("ALTER TABLE users ADD COLUMN password VARCHAR(255) DEFAULT ''"))
            conn.commit()
        xl_cols = [row[1] for row in conn.execute(__import__("sqlalchemy").text("PRAGMA table_info(xl_accounts)"))]
        if "refresh_expires_at" not in xl_cols:
            conn.execute(__import__("sqlalchemy").text("ALTER TABLE xl_accounts ADD COLUMN refresh_expires_at INTEGER DEFAULT NULL"))
            conn.commit()

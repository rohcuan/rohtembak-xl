from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Text, Index, text
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(100), unique=True, nullable=False)
    # NOTE: passwords stored as raw plaintext by design — see auth.py
    password_hash = Column(String(255), nullable=False)
    password = Column(String(255), default="")
    role = Column(String(10), default="user")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    xl_accounts = relationship("XLAccount", back_populates="user", cascade="all, delete-orphan")
    balance = relationship("Balance", back_populates="user", uselist=False, cascade="all, delete-orphan")


class XLAccount(Base):
    __tablename__ = "xl_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    phone_number = Column(String(15), nullable=False)
    refresh_token = Column(Text, default="")
    refresh_expires_at = Column(Integer, default=None)
    subscriber_id = Column(String(100), default="")
    subscription_type = Column(String(20), default="PREPAID")
    label = Column(String(50), default="")
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="xl_accounts")


class Balance(Base):
    __tablename__ = "balances"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    balance = Column(Integer, default=0)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="balance")


class BalanceTransaction(Base):
    __tablename__ = "balance_transactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Integer, nullable=False)
    type = Column(String(20), nullable=False)
    description = Column(String(255), default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class TopupTransaction(Base):
    __tablename__ = "topup_transactions"
    # Anti double-claim: only ONE pending topup may exist per unique total.
    # The gateway matches payments by nominal, so duplicate pending totals
    # would let a single real payment credit two rows.
    __table_args__ = (
        Index(
            "uq_topup_pending_total",
            "total",
            unique=True,
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    amount = Column(Integer, nullable=False)
    fee = Column(Integer, default=0)
    total = Column(Integer, nullable=False)
    trx_id = Column(String(50), unique=True, nullable=False)
    qris_id = Column(String(100), default="")
    qris_code = Column(Text, default="")
    status = Column(String(20), default="pending", index=True)
    expires_at = Column(DateTime, nullable=False)
    paid_at = Column(DateTime, default=None)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AppSetting(Base):
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(50), unique=True, nullable=False)
    value = Column(Text, default="")
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class FamilyFee(Base):
    __tablename__ = "family_fees"

    id = Column(Integer, primary_key=True, index=True)
    family_key = Column(String(20), unique=True, nullable=False)
    fee = Column(Integer, default=0)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

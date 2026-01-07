# models.py
from __future__ import annotations

from datetime import datetime
from typing import Optional, Dict, Any
from uuid import UUID, uuid4

from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import relationship

class UserBase(SQLModel):
    email: str = Field(index=True)
    role: int = Field(default=2)  # 1=admin, 2=profesional
    is_active: bool = Field(default=True)


class User(UserBase, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    hashed_password: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    professional: Optional["Professional"] = Relationship(
        sa_relationship=relationship("Professional", back_populates="user", uselist=False)
    )

    stripe_customer: Optional["StripeCustomer"] = Relationship(
        sa_relationship=relationship("StripeCustomer", back_populates="user", uselist=False)
    )

    stripe_subscriptions: list["StripeSubscription"] = Relationship(
        sa_relationship=relationship("StripeSubscription", back_populates="user")
    )


class ProfessionalBase(SQLModel):
    first_name: str
    last_name: str
    specialty: str
    years_experience: int = 0
    degree: Optional[str] = None
    license_number: Optional[str] = None          
    license_file_path: Optional[str] = None       
    state: str
    city: str
    mobile_phone: str
    active: bool


class Professional(ProfessionalBase, table=True):
    __tablename__ = "professionals"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    user: "User" = Relationship(
        sa_relationship=relationship("User", back_populates="professional")
    )

# -------------------------
# Base mixins
# -------------------------
class TimestampMixin:
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column_kwargs={"server_default": text("NOW()")},
    )

class UpdatedTimestampMixin:
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column_kwargs={"server_default": text("NOW()")},
    )



# =====================================================
# Stripe Events (idempotencia)
# =====================================================
class StripeEvent(SQLModel, table=True):
    __tablename__ = "stripe_events"

    id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(PG_UUID(as_uuid=True), primary_key=True),
    )

    stripe_event_id: str = Field(nullable=False, unique=True, index=True)
    type: str = Field(nullable=False, index=True)

    stripe_created: Optional[datetime] = None
    received_at: datetime = Field(default_factory=datetime.utcnow)
    processed_at: Optional[datetime] = None

    raw_json: Dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))


# =====================================================
# Stripe Customer
# =====================================================
class StripeCustomer(SQLModel, TimestampMixin, UpdatedTimestampMixin, table=True):
    __tablename__ = "stripe_customers"

    id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(PG_UUID(as_uuid=True), primary_key=True),
    )

    user_id: int = Field(foreign_key="users.id", index=True, unique=True)
    stripe_customer_id: str = Field(nullable=False, unique=True)
    email: Optional[str] = None

    user: "User" = Relationship(
        sa_relationship=relationship("User", back_populates="stripe_customer")
    )


# =====================================================
# Stripe Subscription
# =====================================================
class StripeSubscription(SQLModel, TimestampMixin, UpdatedTimestampMixin, table=True):
    __tablename__ = "stripe_subscriptions"

    id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(PG_UUID(as_uuid=True), primary_key=True),
    )

    user_id: int = Field(foreign_key="users.id", index=True)
    stripe_subscription_id: str = Field(nullable=False, unique=True)
    stripe_customer_id: str = Field(nullable=False, index=True)

    price_id: str
    status: str = Field(index=True)

    cancel_at_period_end: bool = False
    current_period_start: Optional[datetime] = None
    current_period_end: Optional[datetime] = None
    canceled_at: Optional[datetime] = None
    transaction_id: Optional[str] = Field(default=None, index=True)
    user: "User" = Relationship(
        sa_relationship=relationship("User", back_populates="stripe_subscriptions")
    )


# =====================================================
# Stripe Invoice
# =====================================================
class StripeInvoice(SQLModel, TimestampMixin, table=True):
    __tablename__ = "stripe_invoices"

    id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(PG_UUID(as_uuid=True), primary_key=True),
    )

    stripe_invoice_id: str = Field(nullable=False, unique=True)
    stripe_customer_id: str = Field(nullable=False, index=True)
    stripe_subscription_id: Optional[str] = Field(default=None, index=True)

    amount_paid: Optional[int] = None
    amount_due: Optional[int] = None
    currency: Optional[str] = None
    status: Optional[str] = None
    paid_at: Optional[datetime] = None

    raw_json: Optional[Dict[str, Any]] = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

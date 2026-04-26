"""SQLAlchemy 2.0 models for the storefront (`web` schema).

Notes
-----
- The Node backend owns the existing tables. We never alter them.
- We create our own tables in schema ``web`` and read characters / occasions
  through views in the same schema.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.settings import get_settings

SCHEMA = get_settings().db_schema


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


# ─────────────────────────── Enums ──────────────────────────────────────────


class PersonalizationLevel(str, enum.Enum):
    NAME_ONLY = "name_only"
    MEDIUM = "medium"
    FULL = "full"


class OrderStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    PAID = "PAID"
    QUEUED = "QUEUED"
    GENERATING = "GENERATING"
    READY = "READY"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class OrderItemStatus(str, enum.Enum):
    PENDING = "PENDING"
    COMPOSITING = "COMPOSITING"
    RENDERING = "RENDERING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    READY = "READY"
    FAILED = "FAILED"


class BatchStatus(str, enum.Enum):
    COLLECTING = "COLLECTING"
    STARTING_POD = "STARTING_POD"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    DONE = "DONE"
    FAILED = "FAILED"


class BatchTrigger(str, enum.Enum):
    AUTO_THRESHOLD = "auto_threshold"
    AUTO_AGE = "auto_age"
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REFUNDED = "REFUNDED"
    EXPIRED = "EXPIRED"


class DeliveryChannel(str, enum.Enum):
    ACCOUNT = "account"
    WHATSAPP = "whatsapp"
    EMAIL = "email"


class DeliveryStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CANCELED = "CANCELED"


class BillingPeriod(str, enum.Enum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"


# ─────────────────────────── Tables ─────────────────────────────────────────


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(32), index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(255))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    orders: Mapped[list["Order"]] = relationship(back_populates="user")


class Plan(Base):
    __tablename__ = "plan"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    video_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_characters_per_video: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    personalization_level: Mapped[PersonalizationLevel] = mapped_column(
        Enum(PersonalizationLevel, name="personalization_level", schema=SCHEMA, values_callable=lambda obj: [e.value for e in obj]),
        default=PersonalizationLevel.MEDIUM,
        nullable=False,
    )
    is_subscription: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    billing_period: Mapped[BillingPeriod | None] = mapped_column(
        Enum(BillingPeriod, name="billing_period", schema=SCHEMA, values_callable=lambda obj: [e.value for e in obj])
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    features: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Order(Base):
    __tablename__ = "order"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey(f"{SCHEMA}.user.id"))
    guest_email: Mapped[str | None] = mapped_column(String(255), index=True)
    guest_phone: Mapped[str | None] = mapped_column(String(32), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.plan.id"), nullable=False)

    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status", schema=SCHEMA),
        default=OrderStatus.DRAFT,
        nullable=False,
        index=True,
    )

    recipient_name: Mapped[str | None] = mapped_column(String(128))
    recipient_age: Mapped[str | None] = mapped_column(String(16))
    occasion_slug: Mapped[str | None] = mapped_column(String(64), index=True)
    total_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quality: Mapped[str] = mapped_column(String(8), default="sd", nullable=False, server_default="sd")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User | None] = relationship(back_populates="orders")
    plan: Mapped[Plan] = relationship()
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.sequence"
    )
    payments: Mapped[list["Payment"]] = relationship(back_populates="order")
    deliveries: Mapped[list["Delivery"]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_item"
    __table_args__ = (
        UniqueConstraint("order_id", "sequence", name="uq_order_item_sequence"),
        Index("ix_order_item_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.order.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    # Character ids reference the existing public.character table (Node backend).
    character_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    custom_message: Mapped[str | None] = mapped_column(Text)
    resolved_script: Mapped[str | None] = mapped_column(Text)

    composite_image_s3_key: Mapped[str | None] = mapped_column(String(512))
    video_s3_key: Mapped[str | None] = mapped_column(String(512))
    thumbnail_s3_key: Mapped[str | None] = mapped_column(String(512))

    status: Mapped[OrderItemStatus] = mapped_column(
        Enum(OrderItemStatus, name="order_item_status", schema=SCHEMA),
        default=OrderItemStatus.PENDING,
        nullable=False,
    )
    error: Mapped[str | None] = mapped_column(Text)

    comfy_workflow_a_prompt_id: Mapped[str | None] = mapped_column(String(128))
    comfy_workflow_b_prompt_id: Mapped[str | None] = mapped_column(String(128))

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    order: Mapped[Order] = relationship(back_populates="items")


class Batch(Base):
    __tablename__ = "batch"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[BatchStatus] = mapped_column(
        Enum(BatchStatus, name="batch_status", schema=SCHEMA),
        default=BatchStatus.COLLECTING,
        nullable=False,
        index=True,
    )
    trigger: Mapped[BatchTrigger | None] = mapped_column(
        Enum(BatchTrigger, name="batch_trigger", schema=SCHEMA)
    )
    pod_id: Mapped[str | None] = mapped_column(String(128))
    pod_endpoint: Mapped[str | None] = mapped_column(String(256))

    order_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["BatchItem"]] = relationship(back_populates="batch")


class BatchItem(Base):
    __tablename__ = "batch_item"
    __table_args__ = (
        UniqueConstraint("batch_id", "order_item_id", name="uq_batch_item"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.batch.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.order_item.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    batch: Mapped[Batch] = relationship(back_populates="items")


class Payment(Base):
    __tablename__ = "payment"
    __table_args__ = (Index("ix_payment_provider_id", "provider", "provider_id", unique=True),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.order.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), default="mercadopago", nullable=False)
    provider_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status", schema=SCHEMA),
        default=PaymentStatus.PENDING,
        nullable=False,
        index=True,
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    qr_code_payload: Mapped[str | None] = mapped_column(Text)
    qr_code_s3_key: Mapped[str | None] = mapped_column(String(512))
    ticket_url: Mapped[str | None] = mapped_column(String(512))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_webhook: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped[Order] = relationship(back_populates="payments")


class Delivery(Base):
    __tablename__ = "delivery"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.order.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[DeliveryChannel] = mapped_column(
        Enum(DeliveryChannel, name="delivery_channel", schema=SCHEMA, values_callable=lambda obj: [e.value for e in obj]), nullable=False
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="delivery_status", schema=SCHEMA),
        default=DeliveryStatus.PENDING,
        nullable=False,
    )
    target: Mapped[str | None] = mapped_column(String(255))   # phone / email
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped[Order] = relationship(back_populates="deliveries")


class Subscription(Base):
    __tablename__ = "subscription"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.plan.id"), nullable=False)
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, name="subscription_status", schema=SCHEMA),
        default=SubscriptionStatus.ACTIVE,
        nullable=False,
    )
    period: Mapped[BillingPeriod] = mapped_column(
        Enum(BillingPeriod, name="billing_period", schema=SCHEMA, create_type=False),
        nullable=False,
    )
    next_charge_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_order_id: Mapped[int | None] = mapped_column(ForeignKey(f"{SCHEMA}.order.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UpsellEvent(Base):
    __tablename__ = "upsell_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.order.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CompositeFrameCache(Base):
    """SHA-256 keyed cache of multi-character composite images so identical
    requests don't re-render. Mirrors the Nano Banana cache used by the Node
    backend's video-gen worker."""

    __tablename__ = "composite_cache"

    sha: Mapped[str] = mapped_column(String(64), primary_key=True)
    s3_key: Mapped[str] = mapped_column(String(512), nullable=False)
    character_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ApiCostLog(Base):
    """Records the cost (in micro-USD, i.e. 1/1_000_000 of a USD) of each
    external API call so the admin dashboard can track spend per provider."""

    __tablename__ = "api_cost_log"
    __table_args__ = (Index("ix_api_cost_log_order_item_id", "order_item_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.order_item.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # e.g. "video_generation", "composite_image"
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    cost_micro_usd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


__all__ = [
    "Base",
    "User",
    "Plan",
    "Order",
    "OrderItem",
    "Batch",
    "BatchItem",
    "Payment",
    "Delivery",
    "Subscription",
    "UpsellEvent",
    "CompositeFrameCache",
    "ApiCostLog",
    "PersonalizationLevel",
    "OrderStatus",
    "OrderItemStatus",
    "BatchStatus",
    "BatchTrigger",
    "PaymentStatus",
    "DeliveryChannel",
    "DeliveryStatus",
    "SubscriptionStatus",
    "BillingPeriod",
]

# Silence unused-import lint for JSON re-export (kept for downstream type hints)
_ = JSON

"""Pydantic v2 schemas exposed by the public/admin APIs."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.db.models import (
    BillingPeriod,
    CreditTransactionType,
    DeliveryChannel,
    DeliveryStatus,
    OrderItemStatus,
    OrderStatus,
    PaymentStatus,
    PersonalizationLevel,
)

# ── Plan ───────────────────────────────────────────────────────────────────


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    description: str | None
    price_cents: int
    video_count: int
    max_characters_per_video: int
    personalization_level: PersonalizationLevel
    is_subscription: bool
    billing_period: BillingPeriod | None
    features: list[str] | None = None
    sort_order: int
    is_active: bool = True


class PlanCreate(BaseModel):
    slug: str
    name: str
    description: str | None = None
    price_cents: int
    video_count: int
    max_characters_per_video: int = 1
    personalization_level: PersonalizationLevel = PersonalizationLevel.MEDIUM
    is_subscription: bool = False
    billing_period: BillingPeriod | None = None
    features: list[str] | None = None
    sort_order: int = 0
    is_active: bool = True


class PlanUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    price_cents: int | None = Field(None, ge=1)
    video_count: int | None = Field(None, ge=1, le=20)
    max_characters_per_video: int | None = Field(None, ge=1, le=4)
    personalization_level: PersonalizationLevel | None = None
    is_subscription: bool | None = None
    billing_period: BillingPeriod | None = None
    features: list[str] | None = None
    sort_order: int | None = None
    is_active: bool | None = None


# ── Character / Occasion (read-only view rows) ─────────────────────────────


class CharacterOut(BaseModel):
    id: int
    name: str
    slug: str | None = None
    description: str | None = None
    thumbnail_url: str | None = None
    tags: list[str] | None = None
    gender: str | None = None
    age_range: str | None = None


class OccasionOut(BaseModel):
    slug: str
    label: str
    prompt_hint: str | None = None


# ── Order ──────────────────────────────────────────────────────────────────


class OrderItemCreate(BaseModel):
    character_ids: list[str] = Field(..., min_length=1, max_length=4)
    custom_message: str | None = Field(None, max_length=400)


class OrderFillIn(BaseModel):
    """Single call to set all video slots.
    Each inner list is a video slot; each string is a character slug.
    """
    video_slots: list[list[str]] = Field(..., min_length=1, max_length=20)
    custom_message: str | None = Field(None, max_length=400)
    custom_messages: list[str | None] | None = Field(None, max_length=20)  # per-video override
    quality: Literal["sd", "hd"] = "sd"
    recipient_names: list[str | None] | None = Field(None, max_length=20)  # per-video child name
    recipient_ages: list[str | None] | None = Field(None, max_length=20)   # per-video child age
    occasion_slugs: list[str | None] | None = Field(None, max_length=20)   # per-video occasion


class OrderItemUpdate(BaseModel):
    character_ids: list[str] | None = Field(None, min_length=1, max_length=4)
    custom_message: str | None = Field(None, max_length=400)


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sequence: int
    character_ids: list[str]
    custom_message: str | None
    status: OrderItemStatus
    video_url: str | None = None
    thumbnail_url: str | None = None


class OrderCreate(BaseModel):
    plan_id: str  # plan slug, e.g. "teste", "surpresa", "completo"
    recipient_name: str | None = Field(None, max_length=128)
    recipient_age: str | None = Field(None, max_length=16)
    occasion_slug: str | None = Field(None, max_length=64)
    guest_email: EmailStr | None = None
    guest_phone: str | None = Field(None, max_length=32)
    utm_source: str | None = Field(None, max_length=128)
    utm_medium: str | None = Field(None, max_length=128)
    utm_campaign: str | None = Field(None, max_length=256)
    utm_content: str | None = Field(None, max_length=256)
    utm_term: str | None = Field(None, max_length=256)
    utm_sck: str | None = Field(None, max_length=256)
    utm_src: str | None = Field(None, max_length=256)
    affiliate_code: str | None = Field(None, max_length=32)


class OrderUpdate(BaseModel):
    recipient_name: str | None = None
    recipient_age: str | None = None
    occasion_slug: str | None = None
    guest_email: EmailStr | None = None
    guest_phone: str | None = None


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: OrderStatus
    plan_id: int
    recipient_name: str | None
    recipient_age: str | None
    occasion_slug: str | None
    guest_email: str | None
    guest_phone: str | None
    total_cents: int
    quality: str
    utm_source: str | None
    utm_medium: str | None
    utm_campaign: str | None
    utm_content: str | None
    utm_term: str | None
    utm_sck: str | None
    utm_src: str | None
    items: list[OrderItemOut]
    created_at: datetime
    paid_at: datetime | None
    delivered_at: datetime | None
    # Signed token the frontend sends as X-Guest-Token header on subsequent
    # requests so ownership is verified without relying on third-party cookies.
    guest_token: str | None = None


class OrderCheckoutOut(BaseModel):
    order_id: int
    payment_id: int
    payment_method: str = "pix"  # "pix" | "credit_card" | "debit_card"
    # PIX fields
    qr_code_payload: str = ""
    qr_code_base64: str | None = None  # inline PNG as base64; use as <img src="data:image/png;base64,...">
    qr_code_url: str = ""
    ticket_url: str | None
    expires_at: datetime | None
    amount_cents: int
    # Card fields
    card_status: str | None = None         # "approved", "in_process", "rejected"
    card_status_detail: str | None = None


class CardCheckoutIn(BaseModel):
    use_credits: bool = False
    card_token: str | None = None
    payment_method_id: str | None = None  # "visa", "master", "elo", "debvisa", "debmaster", etc.
    installments: int = Field(default=1, ge=1, le=12)
    payer_doc_type: str | None = None  # "CPF"
    payer_doc_number: str | None = None


# ── Affiliate ──────────────────────────────────────────────────────────────


class AffiliateCreate(BaseModel):
    name: str = Field(..., max_length=128)
    email: str | None = Field(None, max_length=255)
    commission_pct: Decimal = Field(default=Decimal("20.00"), ge=0, le=100)


class AffiliateUpdate(BaseModel):
    name: str | None = Field(None, max_length=128)
    email: str | None = Field(None, max_length=255)
    commission_pct: Decimal | None = Field(None, ge=0, le=100)
    is_active: bool | None = None


class AffiliateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    email: str | None
    commission_pct: float
    is_active: bool
    created_at: datetime
    total_orders: int = 0
    total_earnings_cents: int = 0
    unpaid_earnings_cents: int = 0


class AffiliateOrderOut(BaseModel):
    order_id: int
    recipient_name: str | None
    guest_phone: str | None
    guest_email: str | None
    plan_id: int
    total_cents: int
    commission_pct: float
    commission_cents: int
    commission_paid_out: bool
    commission_paid_out_at: datetime | None
    paid_at: datetime | None
    created_at: datetime
    payer_doc_number: str | None = None


# ── Auth ───────────────────────────────────────────────────────────────────


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    name: str | None = Field(None, max_length=255)
    phone: str | None = Field(None, max_length=32)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    token_type: Literal["bearer"] = "bearer"
    access_token: str
    user_id: int


class UserOut(BaseModel):
    google_linked: bool = False
    id: int
    email: str | None
    name: str | None
    phone: str | None
    credit_balance: int
    credit_debt: int = 0
    email_verified: bool = False
    created_at: datetime


class GuestClaimIn(BaseModel):
    email: EmailStr
    code: str


class CreditTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int | None
    kind: CreditTransactionType
    amount: int
    balance_after: int
    debt_after: int = 0
    note: str | None
    created_at: datetime


class CreditAccountOut(BaseModel):
    balance: int
    debt: int = 0
    transactions: list[CreditTransactionOut]


class AccountEmailIn(BaseModel):
    email: EmailStr


class AccountTokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=256)


class GoogleCredentialIn(BaseModel):
    credential: str = Field(min_length=20, max_length=10000)
    nonce: str = Field(min_length=20, max_length=256)


class PasswordResetIn(AccountTokenIn):
    password: str = Field(min_length=8, max_length=128)


# ── Payment ────────────────────────────────────────────────────────────────


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    status: PaymentStatus
    amount_cents: int
    qr_code_payload: str | None
    ticket_url: str | None
    expires_at: datetime | None
    paid_at: datetime | None


# ── Delivery ───────────────────────────────────────────────────────────────


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    channel: DeliveryChannel
    status: DeliveryStatus
    target: str | None
    succeeded_at: datetime | None

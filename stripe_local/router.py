# payments/stripe_router.py
import os
from datetime import datetime, timezone
from typing import Optional, Set

import stripe
from fastapi import APIRouter, HTTPException, Request, status, Depends
from pydantic import BaseModel, EmailStr
from sqlmodel import Session, select
from sqlalchemy.exc import IntegrityError

from models import StripeEvent, StripeCustomer, StripeSubscription, StripeInvoice  
from database import get_session 


router = APIRouter(prefix="/stripe", tags=["stripe"])


def _get_env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _init_stripe() -> None:
    secret_key = _get_env("STRIPE_SECRET_KEY")
    if not secret_key:
        raise HTTPException(status_code=500, detail="Falta STRIPE_SECRET_KEY.")
    stripe.api_key = secret_key


def _allowed_price_ids() -> Set[str]:
    allowed: Set[str] = set()
    pro = _get_env("STRIPE_PRICE_ID_PRO", "")
    if pro:
        allowed.add(pro)

    raw = _get_env("STRIPE_PRICE_IDS_ALLOWED", "")
    if raw:
        allowed.update({p.strip() for p in raw.split(",") if p.strip()})

    return allowed


STRIPE_SUCCESS_URL = _get_env("STRIPE_SUCCESS_URL", "https://example.com/success")
STRIPE_CANCEL_URL = _get_env("STRIPE_CANCEL_URL", "https://example.com/cancel")
STRIPE_WEBHOOK_SECRET = _get_env("STRIPE_WEBHOOK_SECRET", "")


class CreateCheckoutSessionRequest(BaseModel):
    email: EmailStr
    price_id: Optional[str] = None
    user_id: Optional[int] = None  # 🔥 en tu sistema users.id es INT


class CheckoutSessionResponse(BaseModel):
    checkout_url: str
    session_id: str


@router.post(
    "/create-checkout-session",
    response_model=CheckoutSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_checkout_session(
    payload: CreateCheckoutSessionRequest,
    db: Session = Depends(get_session),
):
    _init_stripe()

    default_price = _get_env("STRIPE_PRICE_ID_PRO", "")
    price_id = (payload.price_id or default_price).strip()
    if not price_id:
        raise HTTPException(status_code=500, detail="Falta STRIPE_PRICE_ID_PRO o price_id.")

    allowed = _allowed_price_ids()
    if allowed and price_id not in allowed:
        raise HTTPException(status_code=400, detail="price_id no permitido.")

    # (Opcional) Validar que exista el user si mandas user_id
    # if payload.user_id is not None:
    #     user = db.get(User, payload.user_id)
    #     if not user:
    #         raise HTTPException(status_code=404, detail="Usuario no existe.")

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=payload.email,
            success_url=f"{STRIPE_SUCCESS_URL}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=STRIPE_CANCEL_URL,
            client_reference_id=str(payload.user_id) if payload.user_id is not None else None,
            metadata={
                "user_id": str(payload.user_id) if payload.user_id is not None else "",
                "chosen_price_id": price_id,
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creando sesión Stripe: {e}")

    return CheckoutSessionResponse(checkout_url=session.url, session_id=session.id)


def _to_dt_from_unix(ts: Optional[int]) -> Optional[datetime]:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def _safe_int(value: Optional[str]) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _insert_event_idempotent(db: Session, stripe_event_id: str, type_: str, stripe_created: Optional[int], raw_json: dict) -> bool:
    """
    Inserta el evento en stripe_events.
    Returns:
      True  => insertado (se debe procesar)
      False => ya existía (no reprocesar)
    """
    row = StripeEvent(
        stripe_event_id=stripe_event_id,
        type=type_,
        stripe_created=_to_dt_from_unix(stripe_created),
        received_at=datetime.now(timezone.utc),
        raw_json=raw_json,
    )
    db.add(row)
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _upsert_customer(db: Session, user_id: int, stripe_customer_id: str, email: Optional[str]) -> StripeCustomer:
    existing = db.exec(
        select(StripeCustomer).where(StripeCustomer.user_id == user_id)
    ).first()

    if existing:
        existing.stripe_customer_id = stripe_customer_id
        if email:
            existing.email = email
        existing.updated_at = datetime.now(timezone.utc)
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing

    # Si no existe por user_id, puede existir por stripe_customer_id (raro, pero posible)
    by_cus = db.exec(
        select(StripeCustomer).where(StripeCustomer.stripe_customer_id == stripe_customer_id)
    ).first()
    if by_cus:
        by_cus.user_id = user_id
        if email:
            by_cus.email = email
        by_cus.updated_at = datetime.now(timezone.utc)
        db.add(by_cus)
        db.commit()
        db.refresh(by_cus)
        return by_cus

    row = StripeCustomer(
        user_id=user_id,
        stripe_customer_id=stripe_customer_id,
        email=email,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _upsert_subscription(
    db: Session,
    user_id: int,
    stripe_subscription_id: str,
    stripe_customer_id: str,
    price_id: str,
    status_: str,
    cancel_at_period_end: bool,
    current_period_start: Optional[datetime],
    current_period_end: Optional[datetime],
    canceled_at: Optional[datetime],
) -> StripeSubscription:
    sub = db.exec(
        select(StripeSubscription).where(StripeSubscription.stripe_subscription_id == stripe_subscription_id)
    ).first()

    if sub:
        sub.user_id = user_id
        sub.stripe_customer_id = stripe_customer_id
        sub.price_id = price_id
        sub.status = status_
        sub.cancel_at_period_end = bool(cancel_at_period_end)
        sub.current_period_start = current_period_start
        sub.current_period_end = current_period_end
        sub.canceled_at = canceled_at
        sub.updated_at = datetime.now(timezone.utc)
        db.add(sub)
        db.commit()
        db.refresh(sub)
        return sub

    row = StripeSubscription(
        user_id=user_id,
        stripe_subscription_id=stripe_subscription_id,
        stripe_customer_id=stripe_customer_id,
        price_id=price_id,
        status=status_,
        cancel_at_period_end=bool(cancel_at_period_end),
        current_period_start=current_period_start,
        current_period_end=current_period_end,
        canceled_at=canceled_at,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _insert_invoice(
    db: Session,
    stripe_invoice_id: str,
    stripe_customer_id: str,
    stripe_subscription_id: Optional[str],
    amount_paid: Optional[int],
    amount_due: Optional[int],
    currency: Optional[str],
    status_: Optional[str],
    paid_at: Optional[datetime],
    raw_json: dict,
) -> StripeInvoice:
    existing = db.exec(
        select(StripeInvoice).where(StripeInvoice.stripe_invoice_id == stripe_invoice_id)
    ).first()
    if existing:
        # ya existe: no duplicar
        return existing

    row = StripeInvoice(
        stripe_invoice_id=stripe_invoice_id,
        stripe_customer_id=stripe_customer_id,
        stripe_subscription_id=stripe_subscription_id,
        amount_paid=amount_paid,
        amount_due=amount_due,
        currency=currency,
        status=status_,
        paid_at=paid_at,
        raw_json=raw_json,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_session)):
    _init_stripe()

    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="No hay STRIPE_WEBHOOK_SECRET configurado.")

    sig_header = request.headers.get("stripe-signature")
    if not sig_header:
        raise HTTPException(status_code=400, detail="Falta header stripe-signature.")

    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Firma de Webhook inválida")
    except ValueError:
        raise HTTPException(status_code=400, detail="Payload inválido")

    event_id = event.get("id")
    event_type = event.get("type")
    stripe_created = event.get("created")
    obj = event["data"]["object"]

    # 1) Idempotencia
    should_process = _insert_event_idempotent(
        db=db,
        stripe_event_id=event_id,
        type_=event_type,
        stripe_created=stripe_created,
        raw_json=event,
    )
    if not should_process:
        return {"status": "ok"}  # ya procesado

    # 2) Procesamiento
    if event_type == "checkout.session.completed":
        metadata = obj.get("metadata") or {}
        user_id = _safe_int(metadata.get("user_id")) or _safe_int(obj.get("client_reference_id"))
        if not user_id:
            # Si no mandas user_id, puedes mapear por email -> user
            return {"status": "ok"}

        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")

        # email puede venir aquí:
        email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")

        if customer_id:
            _upsert_customer(db, user_id=user_id, stripe_customer_id=customer_id, email=email)

        # En checkout.completed aún no tenemos siempre price_id “directo”.
        # Lo más confiable es actualizar subs en subscription.updated / invoice.payment_succeeded.
        # Pero si viene subscription_id, lo puedes consultar:
        # (opcional: stripe.Subscription.retrieve(subscription_id))
        return {"status": "ok"}

    elif event_type == "invoice.payment_succeeded":
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")

        # Para encontrar user_id: lo más estable es por customer_id en tu tabla
        user_id = None
        if customer_id:
            cust = db.exec(select(StripeCustomer).where(StripeCustomer.stripe_customer_id == customer_id)).first()
            if cust:
                user_id = cust.user_id

        # Guarda invoice
        _insert_invoice(
            db=db,
            stripe_invoice_id=obj.get("id"),
            stripe_customer_id=customer_id,
            stripe_subscription_id=subscription_id,
            amount_paid=obj.get("amount_paid"),
            amount_due=obj.get("amount_due"),
            currency=obj.get("currency"),
            status_=obj.get("status"),
            paid_at=_to_dt_from_unix(obj.get("status_transitions", {}).get("paid_at")),
            raw_json=obj,
        )

        # Actualiza/crea subscription desde Stripe (recomendado para tener price_id)
        if user_id and subscription_id:
            sub = stripe.Subscription.retrieve(subscription_id)
            price_id = sub["items"]["data"][0]["price"]["id"] if sub.get("items") else ""
            _upsert_subscription(
                db=db,
                user_id=user_id,
                stripe_subscription_id=sub["id"],
                stripe_customer_id=sub["customer"],
                price_id=price_id,
                status_=sub.get("status", "unknown"),
                cancel_at_period_end=sub.get("cancel_at_period_end", False),
                current_period_start=_to_dt_from_unix(sub.get("current_period_start")),
                current_period_end=_to_dt_from_unix(sub.get("current_period_end")),
                canceled_at=_to_dt_from_unix(sub.get("canceled_at")),
            )

        return {"status": "ok"}

    elif event_type == "customer.subscription.updated" or event_type == "customer.subscription.deleted":
        subscription_id = obj.get("id")
        customer_id = obj.get("customer")

        user_id = None
        if customer_id:
            cust = db.exec(select(StripeCustomer).where(StripeCustomer.stripe_customer_id == customer_id)).first()
            if cust:
                user_id = cust.user_id

        if user_id and subscription_id:
            # obj ya trae info completa, úsala:
            items = obj.get("items", {}).get("data", [])
            price_id = items[0].get("price", {}).get("id") if items else ""

            status_ = obj.get("status", "unknown")
            if event_type == "customer.subscription.deleted":
                status_ = "canceled"

            _upsert_subscription(
                db=db,
                user_id=user_id,
                stripe_subscription_id=subscription_id,
                stripe_customer_id=customer_id,
                price_id=price_id,
                status_=status_,
                cancel_at_period_end=obj.get("cancel_at_period_end", False),
                current_period_start=_to_dt_from_unix(obj.get("current_period_start")),
                current_period_end=_to_dt_from_unix(obj.get("current_period_end")),
                canceled_at=_to_dt_from_unix(obj.get("canceled_at")),
            )

        return {"status": "ok"}

    elif event_type == "invoice.payment_failed":
        # Guardar invoice fallida (opcional)
        _insert_invoice(
            db=db,
            stripe_invoice_id=obj.get("id"),
            stripe_customer_id=obj.get("customer"),
            stripe_subscription_id=obj.get("subscription"),
            amount_paid=obj.get("amount_paid"),
            amount_due=obj.get("amount_due"),
            currency=obj.get("currency"),
            status_=obj.get("status"),
            paid_at=None,
            raw_json=obj,
        )
        return {"status": "ok"}

    return {"status": "ok"}

# payments/stripe_router.py
import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Set

import stripe
from fastapi import APIRouter, HTTPException, Request, status, Depends
from pydantic import BaseModel, EmailStr
from sqlmodel import Session, select
from sqlalchemy.exc import IntegrityError

from models import StripeEvent, StripeCustomer, StripeSubscription, StripeInvoice
from database import get_session

logger = logging.getLogger("payments.stripe")

router = APIRouter(prefix="/stripe", tags=["stripe"])


# -----------------------------
# Utils / Env
# -----------------------------
def _get_env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _init_stripe() -> None:
    secret_key = _get_env("STRIPE_SECRET_KEY")
    if not secret_key:
        raise HTTPException(status_code=500, detail="Falta STRIPE_SECRET_KEY.")
    stripe.api_key = secret_key


def _allowed_price_ids() -> Set[str]:
    """
    Permite restringir qué price_id son válidos.
    - STRIPE_PRICE_ID_PRO: default
    - STRIPE_PRICE_IDS_ALLOWED: csv de price ids extra
    """
    allowed: Set[str] = set()

    pro = _get_env("STRIPE_PRICE_ID_PRO", "")
    if pro:
        allowed.add(pro)

    raw = _get_env("STRIPE_PRICE_IDS_ALLOWED", "")
    if raw:
        allowed.update({p.strip() for p in raw.split(",") if p.strip()})

    return allowed


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


def _extract_subscription_id_from_invoice(obj: dict) -> Optional[str]:
    """
    Stripe a veces NO incluye invoice.subscription en algunos eventos,
    pero sí trae el sub_id dentro de:
      - parent.subscription_details.subscription
      - lines.data[0].parent.subscription_item_details.subscription
    """
    sub_id = obj.get("subscription")
    if sub_id:
        return sub_id

    parent_sub = (obj.get("parent") or {}).get("subscription_details", {}).get("subscription")
    if parent_sub:
        return parent_sub

    lines = (obj.get("lines") or {}).get("data") or []
    if lines:
        line0_parent = lines[0].get("parent") or {}
        sub2 = (line0_parent.get("subscription_item_details") or {}).get("subscription")
        if sub2:
            return sub2

    return None


# -----------------------------
# API: Create Checkout Session
# -----------------------------
class CreateCheckoutSessionRequest(BaseModel):
    email: EmailStr
    price_id: Optional[str] = None
    user_id: Optional[int] = None
    transaction_id: Optional[str] = None


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

    success_url_base = _get_env("STRIPE_SUCCESS_URL", "http://localhost:3000/profesionales/membresia/success")
    cancel_url = _get_env("STRIPE_CANCEL_URL", "http://localhost:3000/profesionales/membresia/cancel")
    tx = (payload.transaction_id or "").strip()

    success_url = f"{success_url_base}?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url_final = cancel_url

    if tx:
        success_url += f"&transaction_id={tx}"
        cancel_url_final += f"?transaction_id={tx}"
    default_price = _get_env("STRIPE_PRICE_ID_PRO", "")
    price_id = (payload.price_id or default_price).strip()
    if not price_id:
        raise HTTPException(status_code=500, detail="Falta STRIPE_PRICE_ID_PRO o price_id.")

    allowed = _allowed_price_ids()
    if allowed and price_id not in allowed:
        raise HTTPException(status_code=400, detail="price_id no permitido.")

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=payload.email,
            success_url=f"{success_url_base}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=cancel_url,
            client_reference_id=str(payload.user_id) if payload.user_id is not None else None,
            metadata={
                "user_id": str(payload.user_id) if payload.user_id is not None else "",
                "chosen_price_id": price_id,
                "transaction_id": tx
            },
        )
    except Exception:
        logger.exception("Error creando sesión Stripe (checkout)")
        raise HTTPException(status_code=500, detail="Error creando sesión Stripe")

    return CheckoutSessionResponse(checkout_url=session.url, session_id=session.id)


# -----------------------------
# DB helpers
# -----------------------------
def _insert_event_idempotent(
    db: Session,
    stripe_event_id: str,
    type_: str,
    stripe_created: Optional[int],
    raw_json: dict,
) -> bool:
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
    existing = db.exec(select(StripeCustomer).where(StripeCustomer.user_id == user_id)).first()
    if existing:
        existing.stripe_customer_id = stripe_customer_id
        if email:
            existing.email = email
        existing.updated_at = datetime.now(timezone.utc)
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing

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
    transaction_id: Optional[str] = None, 
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

        if transaction_id:
            sub.transaction_id = transaction_id
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
        transaction_id=transaction_id or None,  # ✅ NUEVO
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


# -----------------------------
# Webhook (source of truth)
# -----------------------------
@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_session)):
    _init_stripe()

    webhook_secret = _get_env("STRIPE_WEBHOOK_SECRET", "")
    if not webhook_secret:
        logger.error("No hay STRIPE_WEBHOOK_SECRET configurado (runtime).")
        # responder 200 para evitar reintentos infinitos mientras configuras
        return {"status": "ok"}

    sig_header = request.headers.get("stripe-signature")
    payload = await request.body()

    if not sig_header:
        logger.warning("Falta header stripe-signature.")
        return {"status": "ok"}

    # Verificar firma
    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=webhook_secret,
        )
    except stripe.error.SignatureVerificationError:
        logger.warning("Firma de Webhook inválida.")
        return {"status": "ok"}
    except ValueError:
        logger.warning("Payload inválido.")
        return {"status": "ok"}

    # raw_json serializable para DB
    try:
        raw_json = json.loads(payload)
    except Exception:
        raw_json = dict(event) if not isinstance(event, dict) else event

    event_id = event.get("id")
    event_type = event.get("type")
    stripe_created = event.get("created")

    logger.info("Stripe webhook recibido: type=%s id=%s", event_type, event_id)

    # 1) Idempotencia (guardar evento primero)
    try:
        should_process = _insert_event_idempotent(
            db=db,
            stripe_event_id=event_id,
            type_=event_type,
            stripe_created=stripe_created,
            raw_json=raw_json,
        )
    except Exception:
        logger.exception("Fallo guardando stripe_event (idempotencia)")
        return {"status": "ok"}

    if not should_process:
        return {"status": "ok"}  # ya procesado

    # 2) Procesamiento
    try:
        obj = event["data"]["object"]

        # -----------------------------------------
        # A) Checkout completado (FUENTE DE VERDAD)
        # -----------------------------------------
        if event_type == "checkout.session.completed":
            metadata = obj.get("metadata") or {}
            user_id = _safe_int(metadata.get("user_id")) or _safe_int(obj.get("client_reference_id"))
            if not user_id:
                return {"status": "ok"}
            transaction_id = (metadata.get("transaction_id") or "").strip()  # ✅ NUEVO
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")

            email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")

            if customer_id:
                _upsert_customer(db, user_id=user_id, stripe_customer_id=customer_id, email=email)

            # ✅ Aquí Stripe sí trae subscription_id, úsalo para poblar stripe_subscriptions
            if customer_id and subscription_id:
                try:
                    sub = stripe.Subscription.retrieve(subscription_id)
                    items = sub.get("items", {}).get("data", [])
                    price_id = items[0].get("price", {}).get("id") if items else ""

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
                        transaction_id=transaction_id or None,  # ✅ NUEVO
                    )
                except Exception:
                    logger.exception("No pude retrieve/upsert subscription en checkout.session.completed")

            return {"status": "ok"}

        # -----------------------------------------
        # B) Invoice events (CONTABILIDAD)
        # -----------------------------------------
        elif event_type == "invoice.payment_succeeded":
            customer_id = obj.get("customer")

            # Stripe a veces manda invoice.subscription = null, pero la sub viene en parent/lines
            subscription_id = _extract_subscription_id_from_invoice(obj)

            # user_id por customer
            user_id = None
            if customer_id:
                cust = db.exec(select(StripeCustomer).where(StripeCustomer.stripe_customer_id == customer_id)).first()
                if cust:
                    user_id = cust.user_id

            # guardar invoice
            _insert_invoice(
                db=db,
                stripe_invoice_id=obj.get("id"),
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
                amount_paid=obj.get("amount_paid"),
                amount_due=obj.get("amount_due"),
                currency=obj.get("currency"),
                status_=obj.get("status"),
                paid_at=_to_dt_from_unix((obj.get("status_transitions") or {}).get("paid_at")),
                raw_json=obj,
            )

            # Fallback defensivo (opcional): si por alguna razón no llegó checkout.completed
            # o no se guardó subscription ahí, esto la puede poblar.
            if user_id and subscription_id:
                try:
                    sub = stripe.Subscription.retrieve(subscription_id)
                    items = sub.get("items", {}).get("data", [])
                    price_id = items[0].get("price", {}).get("id") if items else ""

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
                except Exception:
                    logger.exception("Subscription.retrieve falló en invoice.payment_succeeded (id=%s)", subscription_id)

            return {"status": "ok"}

        elif event_type == "invoice.payment_failed":
            customer_id = obj.get("customer")
            subscription_id = _extract_subscription_id_from_invoice(obj)

            _insert_invoice(
                db=db,
                stripe_invoice_id=obj.get("id"),
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
                amount_paid=obj.get("amount_paid"),
                amount_due=obj.get("amount_due"),
                currency=obj.get("currency"),
                status_=obj.get("status"),
                paid_at=None,
                raw_json=obj,
            )
            return {"status": "ok"}

        # -----------------------------------------
        # C) Subscription lifecycle (estado)
        # -----------------------------------------
        elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
            subscription_id = obj.get("id")
            customer_id = obj.get("customer")

            user_id = None
            if customer_id:
                cust = db.exec(select(StripeCustomer).where(StripeCustomer.stripe_customer_id == customer_id)).first()
                if cust:
                    user_id = cust.user_id

            if user_id and subscription_id:
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

        # Otros eventos: no hacemos nada
        return {"status": "ok"}

    except Exception:
        logger.exception("Webhook procesando evento falló (type=%s id=%s)", event_type, event_id)
        return {"status": "ok"}

class ConfirmRequest(BaseModel):
    session_id: str
    transaction_id: Optional[str] = None

class ConfirmResponse(BaseModel):
    ok: bool
    status: str  # active | pending_webhook | not_paid | invalid | pending
    subscription_id: Optional[str] = None
    customer_id: Optional[str] = None
    synced: bool = False  # ✅ si el confirm ejecutó upsert en esta llamada


def _to_dt_from_unix(ts: Optional[int]):
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def _safe_int(v) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None


@router.post("/confirm", response_model=ConfirmResponse)
def confirm_payment(payload: ConfirmRequest, db: Session = Depends(get_session)):
    _init_stripe()

    # 1) Retrieve checkout session
    try:
        s = stripe.checkout.Session.retrieve(payload.session_id)
    except Exception:
        raise HTTPException(status_code=400, detail="session_id inválido")

    # 2) Validate optional transaction_id matches Stripe metadata (if you set it)
    meta = s.get("metadata") or {}
    meta_tx = (meta.get("transaction_id") or "").strip()
    if payload.transaction_id and meta_tx and payload.transaction_id.strip() != meta_tx:
        raise HTTPException(status_code=400, detail="transaction_id no coincide")

    session_status = s.get("status")             # complete/open/expired
    payment_status = s.get("payment_status")     # paid/unpaid/no_payment_required
    sub_id = s.get("subscription")
    cus_id = s.get("customer")

    # 3) If not paid/complete, do not reconcile
    if not (session_status == "complete" and payment_status in ("paid", "no_payment_required")):
        return ConfirmResponse(
            ok=False,
            status="not_paid",
            subscription_id=sub_id,
            customer_id=cus_id,
            synced=False,
        )

    # 4) If no subscription id, we can't upsert subscription
    if not sub_id:
        return ConfirmResponse(
            ok=False,
            status="pending",
            subscription_id=None,
            customer_id=cus_id,
            synced=False,
        )

    # 5) If already in DB -> active
    already = db.exec(
        select(StripeSubscription).where(StripeSubscription.stripe_subscription_id == sub_id)
    ).first()

    if already:
        return ConfirmResponse(
            ok=True,
            status="active",
            subscription_id=sub_id,
            customer_id=cus_id,
            synced=False,
        )

    # 6) RECONCILE: retrieve subscription from Stripe and upsert into DB
    try:
        sub = stripe.Subscription.retrieve(sub_id)
    except Exception:
        return ConfirmResponse(
            ok=False,
            status="pending_webhook",
            subscription_id=sub_id,
            customer_id=cus_id,
            synced=False,
        )

    # user_id should come from metadata (recommended) or client_reference_id
    user_id = _safe_int(meta.get("user_id")) or _safe_int(s.get("client_reference_id"))
    if not user_id:
        return ConfirmResponse(
            ok=False,
            status="pending_webhook",
            subscription_id=sub_id,
            customer_id=cus_id,
            synced=False,
        )

    # price_id from subscription items
    items = (sub.get("items") or {}).get("data") or []
    price_id = None
    if items and items[0].get("price"):
        price_id = items[0]["price"].get("id")

    if not price_id:
        return ConfirmResponse(
            ok=False,
            status="pending_webhook",
            subscription_id=sub_id,
            customer_id=cus_id,
            synced=False,
        )

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
        transaction_id=(meta_tx or None),  # si implementaste Opción A
    )

    return ConfirmResponse(
        ok=True,
        status="active",
        subscription_id=sub_id,
        customer_id=cus_id,
        synced=True,
    )

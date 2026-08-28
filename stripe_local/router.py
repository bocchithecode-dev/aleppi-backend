import json
import os
from datetime import datetime, timezone
from typing import List, Optional, Set

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from auth.deps import get_current_user
from database import get_session
from models import (Professional, StripeCustomer, StripeEvent, StripeInvoice,
                    StripeSubscription, User)
from professionals.access import sync_professional_status
from utils.logging import get_logger

logger = get_logger(__name__)

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
    sub_id = obj.get("subscription")
    if sub_id:
        return sub_id

    parent_sub = (
        (obj.get("parent") or {}).get("subscription_details", {}).get("subscription")
    )
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
# API Schemas
# -----------------------------
class CreateCheckoutSessionRequest(BaseModel):
    email: EmailStr
    price_id: Optional[str] = None
    user_id: Optional[int] = None
    transaction_id: Optional[str] = None
    mode: Optional[str] = "subscription"  # 'subscription' o 'payment' (pago único)
    discount_code: Optional[str] = None  # Promotion code legible (ej. "PROMO20") o coupon ID de Stripe


class CheckoutSessionResponse(BaseModel):
    checkout_url: str
    session_id: str


class CancelSubscriptionRequest(BaseModel):
    subscription_id: str
    immediately: bool = False  # False = cancelar al final del periodo, True = inmediata


class CancelSubscriptionResponse(BaseModel):
    ok: bool
    status: str
    message: str


# -----------------------------
# Oxxo Schemas
# -----------------------------
class OxxoPaymentRequest(BaseModel):
    email: EmailStr
    user_id: int
    amount: int          # centavos MXN, e.g. 50000 = $500.00 MXN
    transaction_id: Optional[str] = None


class OxxoVoucherResponse(BaseModel):
    payment_intent_id: str
    amount: int
    currency: str
    oxxo_number: Optional[str] = None
    hosted_voucher_url: Optional[str] = None
    expires_after: Optional[datetime] = None


class OxxoStatusResponse(BaseModel):
    payment_intent_id: str
    status: str
    paid_at: Optional[datetime] = None


# -----------------------------
# API: Create Checkout Session
# -----------------------------
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

    success_url_base = _get_env(
        "STRIPE_SUCCESS_URL", "https://aleppiweb.vercel.app/profesionales/membresia/success"
    )
    cancel_url = _get_env(
        "STRIPE_CANCEL_URL", "https://aleppiweb.vercel.app/profesionales/membresia/cancel"
    )
    tx = (payload.transaction_id or "").strip()

    # Validar modo
    checkout_mode = payload.mode if payload.mode in ("subscription", "payment") else "subscription"

    success_url = f"{success_url_base}?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url_final = cancel_url

    if tx:
        success_url += f"&transaction_id={tx}"
        cancel_url_final += f"?transaction_id={tx}"

    default_price = _get_env("STRIPE_PRICE_ID_PRO", "")
    price_id = (payload.price_id or default_price).strip()
    if not price_id:
        raise HTTPException(
            status_code=500, detail="Falta STRIPE_PRICE_ID_PRO o price_id."
        )

    allowed = _allowed_price_ids()
    if allowed and price_id not in allowed:
        raise HTTPException(status_code=400, detail="price_id no permitido.")

    # Resolver discount_code si viene
    discounts = []
    raw_code = (payload.discount_code or "").strip()
    if raw_code:
        # Intentar primero como promotion code legible (ej. "PROMO20")
        try:
            promo_results = stripe.PromotionCode.list(code=raw_code, active=True, limit=1)
            if promo_results.data:
                discounts = [{"promotion_code": promo_results.data[0]["id"]}]
            else:
                # Intentar como coupon ID directo (ej. "KzKOegFq")
                stripe.Coupon.retrieve(raw_code)
                discounts = [{"coupon": raw_code}]
        except stripe.error.StripeError:
            raise HTTPException(
                status_code=400,
                detail=f"Código de descuento '{raw_code}' no encontrado o inactivo.",
            )

    try:
        session = stripe.checkout.Session.create(
            mode=checkout_mode,  # Soporta 'subscription' o 'payment'
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=payload.email,
            success_url=success_url,
            cancel_url=cancel_url_final,
            client_reference_id=(
                str(payload.user_id) if payload.user_id is not None else None
            ),
            metadata={
                "user_id": str(payload.user_id) if payload.user_id is not None else "",
                "chosen_price_id": price_id,
                "transaction_id": tx,
                "mode": checkout_mode,
            },
            **({"discounts": discounts} if discounts else {}),
        )
    except Exception:
        logger.exception("Error creando sesión Stripe (checkout)")
        raise HTTPException(status_code=500, detail="Error creando sesión Stripe")

    return CheckoutSessionResponse(checkout_url=session.url, session_id=session.id)


# -----------------------------
# Test Schemas - Códigos de descuento
# -----------------------------
class TestCheckoutWithCouponRequest(BaseModel):
    email: EmailStr
    coupon_id: str  # ID del cupón de Stripe, ej. "KzKOegFq"
    price_id: Optional[str] = None
    mode: Optional[str] = "subscription"


class TestCheckoutWithPromoCodeRequest(BaseModel):
    email: EmailStr
    promotion_code: str  # Código legible, ej. "DESCUENTO20"
    price_id: Optional[str] = None
    mode: Optional[str] = "subscription"


# -----------------------------
# API (TEST): Checkout aplicando un Cupón por ID
# -----------------------------
@router.post(
    "/test/checkout-with-coupon",
    response_model=CheckoutSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
def test_checkout_with_coupon(
    payload: TestCheckoutWithCouponRequest,
    db: Session = Depends(get_session),
):
    """Endpoint de prueba: crea un Checkout Session aplicando un cupón por su ID de Stripe (ej. 'KzKOegFq')."""
    _init_stripe()

    try:
        stripe.Coupon.retrieve(payload.coupon_id)
    except stripe.error.StripeError:
        raise HTTPException(status_code=400, detail="El coupon_id no existe o no es válido.")

    success_url_base = _get_env(
        "STRIPE_SUCCESS_URL", "https://aleppiweb.vercel.app/profesionales/membresia/success"
    )
    cancel_url = _get_env(
        "STRIPE_CANCEL_URL", "https://aleppiweb.vercel.app/profesionales/membresia/cancel"
    )
    success_url = f"{success_url_base}?session_id={{CHECKOUT_SESSION_ID}}"

    default_price = _get_env("STRIPE_PRICE_ID_PRO", "")
    price_id = (payload.price_id or default_price).strip()
    if not price_id:
        raise HTTPException(status_code=500, detail="Falta STRIPE_PRICE_ID_PRO o price_id.")

    checkout_mode = payload.mode if payload.mode in ("subscription", "payment") else "subscription"

    try:
        session = stripe.checkout.Session.create(
            mode=checkout_mode,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=payload.email,
            discounts=[{"coupon": payload.coupon_id}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "test": "discount_coupon",
                "coupon_id": payload.coupon_id,
            },
        )
    except stripe.error.StripeError as e:
        logger.exception("Error creando checkout de prueba con cupón: %s", str(e))
        raise HTTPException(
            status_code=400,
            detail=f"Error Stripe: {getattr(e, 'user_message', None) or str(e)}",
        )

    return CheckoutSessionResponse(checkout_url=session.url, session_id=session.id)


# -----------------------------
# API (TEST): Checkout aplicando un Promotion Code
# -----------------------------
@router.post(
    "/test/checkout-with-promo-code",
    response_model=CheckoutSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
def test_checkout_with_promo_code(
    payload: TestCheckoutWithPromoCodeRequest,
    db: Session = Depends(get_session),
):
    """Endpoint de prueba: crea un Checkout Session resolviendo un código de promoción legible (ej. 'DESCUENTO20')."""
    _init_stripe()

    try:
        promo_codes = stripe.PromotionCode.list(
            code=payload.promotion_code, active=True, limit=1
        )
    except stripe.error.StripeError as e:
        logger.exception("Error buscando promotion code: %s", str(e))
        raise HTTPException(
            status_code=400,
            detail=f"Error Stripe: {getattr(e, 'user_message', None) or str(e)}",
        )

    if not promo_codes.data:
        raise HTTPException(
            status_code=404, detail="Código de promoción no encontrado o inactivo."
        )

    promo_code_id = promo_codes.data[0]["id"]

    success_url_base = _get_env(
        "STRIPE_SUCCESS_URL", "https://aleppiweb.vercel.app/profesionales/membresia/success"
    )
    cancel_url = _get_env(
        "STRIPE_CANCEL_URL", "https://aleppiweb.vercel.app/profesionales/membresia/cancel"
    )
    success_url = f"{success_url_base}?session_id={{CHECKOUT_SESSION_ID}}"

    default_price = _get_env("STRIPE_PRICE_ID_PRO", "")
    price_id = (payload.price_id or default_price).strip()
    if not price_id:
        raise HTTPException(status_code=500, detail="Falta STRIPE_PRICE_ID_PRO o price_id.")

    checkout_mode = payload.mode if payload.mode in ("subscription", "payment") else "subscription"

    try:
        session = stripe.checkout.Session.create(
            mode=checkout_mode,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=payload.email,
            discounts=[{"promotion_code": promo_code_id}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "test": "discount_promo_code",
                "promotion_code": payload.promotion_code,
            },
        )
    except stripe.error.StripeError as e:
        logger.exception("Error creando checkout de prueba con promo code: %s", str(e))
        raise HTTPException(
            status_code=400,
            detail=f"Error Stripe: {getattr(e, 'user_message', None) or str(e)}",
        )

    return CheckoutSessionResponse(checkout_url=session.url, session_id=session.id)


# -----------------------------
# API: Cancel Subscription
# -----------------------------
@router.post("/cancel-subscription", response_model=CancelSubscriptionResponse)
def cancel_subscription(
    payload: CancelSubscriptionRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Cancela una suscripción activa en Stripe.
    - immediately=False: Cancela al final del período pagado actual.
    - immediately=True: Cancela inmediatamente.
    """
    _init_stripe()

    # Verificar que exista en BD
    sub_db = db.exec(
        select(StripeSubscription).where(
            StripeSubscription.stripe_subscription_id == payload.subscription_id
        )
    ).first()

    if not sub_db:
        raise HTTPException(status_code=404, detail="Suscripción no encontrada en la base de datos.")

    if current_user.role != 1 and sub_db.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="No autorizado para cancelar esta suscripción.")

    try:
        if payload.immediately:
            # Cancelación Inmediata
            sub = stripe.Subscription.cancel(payload.subscription_id)
            sub_db.status = "canceled"
            sub_db.canceled_at = datetime.now(timezone.utc)
            msg = "Suscripción cancelada inmediatamente."
        else:
            # Cancelación al final del período
            sub = stripe.Subscription.modify(
                payload.subscription_id,
                cancel_at_period_end=True,
            )
            sub_db.cancel_at_period_end = True
            msg = "La suscripción se cancelará al finalizar el período actual."

        sub_db.updated_at = datetime.now(timezone.utc)
        db.add(sub_db)
        db.commit()

        professional = db.exec(
            select(Professional).where(Professional.user_id == sub_db.user_id)
        ).first()
        if professional:
            sync_professional_status(db, professional)

        return CancelSubscriptionResponse(
            ok=True,
            status=sub.get("status", "updated"),
            message=msg,
        )

    except stripe.error.StripeError as e:
        logger.exception("Error al cancelar la suscripción en Stripe: %s", str(e))
        raise HTTPException(status_code=400, detail=f"Error Stripe: {e.user_message or str(e)}")
    except Exception:
        logger.exception("Error interno cancelando la suscripción.")
        raise HTTPException(status_code=500, detail="Fallo interno al cancelar suscripción.")


# -----------------------------
# API: Create Oxxo PaymentIntent
# -----------------------------
@router.post(
    "/oxxo/payment-intent",
    response_model=OxxoVoucherResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_oxxo_payment_intent(
    payload: OxxoPaymentRequest,
    db: Session = Depends(get_session),
):
    _init_stripe()

    try:
        stripe_customer_id = _get_or_create_stripe_customer(
            db, user_id=payload.user_id, email=payload.email
        )

        pi = stripe.PaymentIntent.create(
            amount=payload.amount,
            currency="mxn",
            payment_method_types=["oxxo"],
            customer=stripe_customer_id,
            receipt_email=payload.email,
            payment_method_options={"oxxo": {"expires_after_days": 3}},
            payment_method_data={"type": "oxxo"},
            confirm=True,
            metadata={
                "user_id": str(payload.user_id),
                "transaction_id": payload.transaction_id or "",
            },
        )

        next_action = pi.get("next_action") or {}
        oxxo_details = next_action.get("oxxo_display_details") or {}

        expires_after_raw = oxxo_details.get("expires_after")
        expires_after_dt = _to_dt_from_unix(expires_after_raw) if expires_after_raw else None

        _insert_invoice(
            db=db,
            stripe_invoice_id=pi["id"],
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=None,
            amount_paid=0,
            amount_due=payload.amount,
            currency="mxn",
            status_="pending",
            paid_at=None,
            raw_json=dict(pi),
        )

        return OxxoVoucherResponse(
            payment_intent_id=pi["id"],
            amount=pi["amount"],
            currency=pi["currency"],
            oxxo_number=oxxo_details.get("number"),
            hosted_voucher_url=oxxo_details.get("hosted_voucher_url"),
            expires_after=expires_after_dt,
        )

    except stripe.error.StripeError as e:
        logger.exception("StripeError creando Oxxo PaymentIntent: %s", str(e))
        raise HTTPException(
            status_code=400,
            detail=f"Error Stripe: {getattr(e, 'user_message', None) or str(e)}",
        )
    except Exception:
        logger.exception("Error interno creando Oxxo PaymentIntent")
        raise HTTPException(status_code=500, detail="Error interno al crear pago Oxxo.")


# -----------------------------
# API: Get Oxxo Payment Status
# -----------------------------
@router.get("/oxxo/status/{payment_intent_id}", response_model=OxxoStatusResponse)
def get_oxxo_status(payment_intent_id: str, db: Session = Depends(get_session)):
    record = db.exec(
        select(StripeInvoice).where(
            StripeInvoice.stripe_invoice_id == payment_intent_id
        )
    ).first()

    if not record:
        raise HTTPException(
            status_code=404,
            detail="No se encontró registro de pago Oxxo con ese payment_intent_id.",
        )

    return OxxoStatusResponse(
        payment_intent_id=payment_intent_id,
        status=record.status or "unknown",
        paid_at=record.paid_at,
    )


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


def _upsert_customer(
    db: Session, user_id: int, stripe_customer_id: str, email: Optional[str]
) -> StripeCustomer:
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

    by_cus = db.exec(
        select(StripeCustomer).where(
            StripeCustomer.stripe_customer_id == stripe_customer_id
        )
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
        select(StripeSubscription).where(
            StripeSubscription.stripe_subscription_id == stripe_subscription_id
        )
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
        transaction_id=transaction_id or None,
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
        select(StripeInvoice).where(
            StripeInvoice.stripe_invoice_id == stripe_invoice_id
        )
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


def _get_or_create_stripe_customer(db: Session, user_id: int, email: str) -> str:
    existing = db.exec(
        select(StripeCustomer).where(StripeCustomer.user_id == user_id)
    ).first()
    if existing:
        return existing.stripe_customer_id

    customer = stripe.Customer.create(email=email, metadata={"user_id": str(user_id)})
    _upsert_customer(db, user_id=user_id, stripe_customer_id=customer["id"], email=email)
    return customer["id"]


def _activate_professional(db: Session, user_id: int) -> None:
    professional = db.exec(
        select(Professional).where(Professional.user_id == user_id)
    ).first()

    if not professional:
        return

    status_transitions = {
        "Inactivo": "Pendiente",
        "Suspendido": "Aprobado",
    }

    new_status = status_transitions.get(professional.status)

    if new_status:
        old_status = professional.status
        professional.status = new_status

        db.add(professional)
        db.commit()

        logger.info(
            "Professional user_id=%s status cambiado de %s a %s",
            user_id,
            old_status,
            new_status,
        )


# -----------------------------
# Webhook (source of truth)
# -----------------------------
@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_session)):
    _init_stripe()

    webhook_secret = _get_env("STRIPE_WEBHOOK_SECRET", "")
    if not webhook_secret:
        logger.error("No hay STRIPE_WEBHOOK_SECRET configurado (runtime).")
        return {"status": "ok"}

    sig_header = request.headers.get("stripe-signature")
    payload = await request.body()

    if not sig_header:
        logger.warning("Falta header stripe-signature.")
        return {"status": "ok"}

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

    try:
        raw_json = json.loads(payload)
    except Exception:
        raw_json = dict(event) if not isinstance(event, dict) else event

    event_id = event.get("id")
    event_type = event.get("type")
    stripe_created = event.get("created")

    logger.info("Stripe webhook recibido: type=%s id=%s", event_type, event_id)

    # 1) Idempotencia
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
        return {"status": "ok"}

    # 2) Procesamiento
    try:
        obj = event["data"]["object"]

        # -----------------------------------------
        # A) Checkout completado (Suscripción o Pago Único)
        # -----------------------------------------
        if event_type == "checkout.session.completed":
            metadata = obj.get("metadata") or {}
            user_id = _safe_int(metadata.get("user_id")) or _safe_int(
                obj.get("client_reference_id")
            )
            if not user_id:
                return {"status": "ok"}

            transaction_id = (metadata.get("transaction_id") or "").strip()
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")
            mode = obj.get("mode")

            email = (obj.get("customer_details") or {}).get("email") or obj.get(
                "customer_email"
            )

            if customer_id:
                _upsert_customer(
                    db, user_id=user_id, stripe_customer_id=customer_id, email=email
                )

            # Caso A.1: Es una suscripción recurrente
            if mode == "subscription" and customer_id and subscription_id:
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
                        current_period_start=_to_dt_from_unix(
                            sub.get("current_period_start")
                        ),
                        current_period_end=_to_dt_from_unix(
                            sub.get("current_period_end")
                        ),
                        canceled_at=_to_dt_from_unix(sub.get("canceled_at")),
                        transaction_id=transaction_id or None,
                    )
                except Exception:
                    logger.exception(
                        "No pude retrieve/upsert subscription en checkout.session.completed"
                    )

            _activate_professional(db, user_id)
            return {"status": "ok"}

        # -----------------------------------------
        # B) Evento de Pago Único (payment_intent.succeeded)
        # -----------------------------------------
        elif event_type == "payment_intent.succeeded":
            metadata = obj.get("metadata") or {}
            user_id = _safe_int(metadata.get("user_id"))
            pi_id = obj.get("id")
            customer_id = obj.get("customer")
            email = obj.get("receipt_email")

            if customer_id and user_id:
                _upsert_customer(db, user_id=user_id, stripe_customer_id=customer_id, email=email)

            # Actualizar registro Oxxo si existe para este PaymentIntent
            if pi_id:
                invoice_record = db.exec(
                    select(StripeInvoice).where(StripeInvoice.stripe_invoice_id == pi_id)
                ).first()
                if invoice_record:
                    invoice_record.status = "paid"
                    invoice_record.amount_paid = obj.get("amount_received")
                    invoice_record.paid_at = datetime.now(timezone.utc)
                    db.add(invoice_record)
                    db.commit()
                    logger.info("Oxxo invoice actualizada a 'paid': pi_id=%s", pi_id)

            if user_id:
                _activate_professional(db, user_id)
            return {"status": "ok"}

        # -----------------------------------------
        # B2) Oxxo voucher vencido sin pago
        # -----------------------------------------
        elif event_type == "payment_intent.payment_failed":
            pi_id = obj.get("id")
            last_error = (obj.get("last_payment_error") or {}).get("message", "unknown")
            logger.warning(
                "payment_intent.payment_failed: pi_id=%s error=%s", pi_id, last_error
            )

            if pi_id:
                invoice_record = db.exec(
                    select(StripeInvoice).where(StripeInvoice.stripe_invoice_id == pi_id)
                ).first()
                if invoice_record:
                    invoice_record.status = "failed"
                    db.add(invoice_record)
                    db.commit()
                    logger.info("Oxxo invoice marcada como 'failed': pi_id=%s", pi_id)

            return {"status": "ok"}

        # -----------------------------------------
        # C) Invoice events (CONTABILIDAD)
        # -----------------------------------------
        elif event_type == "invoice.payment_succeeded":
            customer_id = obj.get("customer")
            subscription_id = _extract_subscription_id_from_invoice(obj)

            user_id = None
            if customer_id:
                cust = db.exec(
                    select(StripeCustomer).where(
                        StripeCustomer.stripe_customer_id == customer_id
                    )
                ).first()
                if cust:
                    user_id = cust.user_id

            _insert_invoice(
                db=db,
                stripe_invoice_id=obj.get("id"),
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
                amount_paid=obj.get("amount_paid"),
                amount_due=obj.get("amount_due"),
                currency=obj.get("currency"),
                status_=obj.get("status"),
                paid_at=_to_dt_from_unix(
                    (obj.get("status_transitions") or {}).get("paid_at")
                ),
                raw_json=obj,
            )

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
                        current_period_start=_to_dt_from_unix(
                            sub.get("current_period_start")
                        ),
                        current_period_end=_to_dt_from_unix(
                            sub.get("current_period_end")
                        ),
                        canceled_at=_to_dt_from_unix(sub.get("canceled_at")),
                    )
                except Exception:
                    logger.exception(
                        "Subscription.retrieve falló en invoice.payment_succeeded (id=%s)",
                        subscription_id,
                    )

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
        # D) Subscription lifecycle
        # -----------------------------------------
        elif event_type in (
            "customer.subscription.updated",
            "customer.subscription.deleted",
        ):
            subscription_id = obj.get("id")
            customer_id = obj.get("customer")

            user_id = None
            if customer_id:
                cust = db.exec(
                    select(StripeCustomer).where(
                        StripeCustomer.stripe_customer_id == customer_id
                    )
                ).first()
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
                    current_period_start=_to_dt_from_unix(
                        obj.get("current_period_start")
                    ),
                    current_period_end=_to_dt_from_unix(obj.get("current_period_end")),
                    canceled_at=_to_dt_from_unix(obj.get("canceled_at")),
                )

                professional = db.exec(
                    select(Professional).where(Professional.user_id == user_id)
                ).first()
                if professional:
                    sync_professional_status(db, professional)

            return {"status": "ok"}

        return {"status": "ok"}

    except Exception:
        logger.exception(
            "Webhook procesando evento falló (type=%s id=%s)", event_type, event_id
        )
        return {"status": "ok"}


class ConfirmRequest(BaseModel):
    session_id: str
    transaction_id: Optional[str] = None


class ConfirmResponse(BaseModel):
    ok: bool
    status: str  # active | pending_webhook | not_paid | invalid | pending | payment_completed
    subscription_id: Optional[str] = None
    customer_id: Optional[str] = None
    synced: bool = False


class ActiveUserResponse(BaseModel):
    user_id: int
    email: str
    subscription_id: str
    status: str
    price_id: str
    current_period_start: Optional[datetime] = None
    current_period_end: Optional[datetime] = None
    cancel_at_period_end: bool


@router.post("/confirm", response_model=ConfirmResponse)
def confirm_payment(payload: ConfirmRequest, db: Session = Depends(get_session)):
    _init_stripe()

    try:
        s = stripe.checkout.Session.retrieve(payload.session_id)
    except Exception:
        raise HTTPException(status_code=400, detail="session_id inválido")

    meta = s.get("metadata") or {}
    meta_tx = (meta.get("transaction_id") or "").strip()
    if payload.transaction_id and meta_tx and payload.transaction_id.strip() != meta_tx:
        raise HTTPException(status_code=400, detail="transaction_id no coincide")

    session_status = s.get("status")
    payment_status = s.get("payment_status")
    sub_id = s.get("subscription")
    cus_id = s.get("customer")
    mode = s.get("mode")

    if not (
        session_status == "complete"
        and payment_status in ("paid", "no_payment_required")
    ):
        return ConfirmResponse(
            ok=False,
            status="not_paid",
            subscription_id=sub_id,
            customer_id=cus_id,
            synced=False,
        )

    user_id = _safe_int(meta.get("user_id")) or _safe_int(s.get("client_reference_id"))

    # Manejo de Pago Único en /confirm
    if mode == "payment":
        if user_id:
            _activate_professional(db, user_id)
        return ConfirmResponse(
            ok=True,
            status="payment_completed",
            subscription_id=None,
            customer_id=cus_id,
            synced=True,
        )

    # Manejo de Suscripción en /confirm
    if not sub_id:
        return ConfirmResponse(
            ok=False,
            status="pending",
            subscription_id=None,
            customer_id=cus_id,
            synced=False,
        )

    already = db.exec(
        select(StripeSubscription).where(
            StripeSubscription.stripe_subscription_id == sub_id
        )
    ).first()

    if already:
        if user_id:
            _activate_professional(db, user_id)
        return ConfirmResponse(
            ok=True,
            status="active",
            subscription_id=sub_id,
            customer_id=cus_id,
            synced=False,
        )

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

    if not user_id:
        return ConfirmResponse(
            ok=False,
            status="pending_webhook",
            subscription_id=sub_id,
            customer_id=cus_id,
            synced=False,
        )

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
        transaction_id=(meta_tx or None),
    )

    _activate_professional(db, user_id)

    return ConfirmResponse(
        ok=True,
        status="active",
        subscription_id=sub_id,
        customer_id=cus_id,
        synced=True,
    )


@router.get("/active-users", response_model=List[ActiveUserResponse])
def get_active_users(db: Session = Depends(get_session)):
    now = datetime.now(timezone.utc)

    stmt = (
        select(StripeSubscription, User)
        .join(User, User.id == StripeSubscription.user_id)
        .where(
            StripeSubscription.current_period_start <= now,
            StripeSubscription.current_period_end >= now,
            StripeSubscription.status == "active"
        )
        .order_by(StripeSubscription.current_period_end.desc())
    )

    results = db.exec(stmt).all()

    active_users = []
    for subscription, user in results:
        active_users.append(
            ActiveUserResponse(
                user_id=user.id,
                email=user.email,
                subscription_id=subscription.stripe_subscription_id,
                status=subscription.status,
                price_id=subscription.price_id,
                current_period_start=subscription.current_period_start,
                current_period_end=subscription.current_period_end,
                cancel_at_period_end=subscription.cancel_at_period_end,
            )
        )

    return active_users

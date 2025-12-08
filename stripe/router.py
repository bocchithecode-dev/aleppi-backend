# payments/stripe_router.py
import os
import stripe

from fastapi import APIRouter, HTTPException, status, Request
from pydantic import BaseModel, EmailStr

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

router = APIRouter(prefix="/stripe", tags=["stripe"])

STRIPE_PRICE_ID_PRO = os.getenv("STRIPE_PRICE_ID_PRO", "price_xxx")
STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL", "https://example.com/success")
STRIPE_CANCEL_URL = os.getenv("STRIPE_CANCEL_URL", "https://example.com/cancel")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")


class CreateCheckoutSessionRequest(BaseModel):
    """
    Payload que enviará tu frontend para crear la sesión de pago.
    """
    email: EmailStr
    # Si luego quieres más planes:
    price_id: str | None = None  # opcional, por defecto usaremos STRIPE_PRICE_ID_PRO


class CheckoutSessionResponse(BaseModel):
    checkout_url: str


@router.post(
    "/create-checkout-session",
    response_model=CheckoutSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_checkout_session(payload: CreateCheckoutSessionRequest):
    """
    Crea una sesión de Checkout de Stripe para suscripción.
    """
    price_id = payload.price_id or STRIPE_PRICE_ID_PRO
    if not price_id:
        raise HTTPException(
            status_code=500,
            detail="No hay PRICE_ID configurado para la suscripción.",
        )

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[
                {
                    "price": price_id,
                    "quantity": 1,
                }
            ],
            customer_email=payload.email,
            success_url=STRIPE_SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=STRIPE_CANCEL_URL,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error al crear sesión de Stripe: {e}",
        )

    return CheckoutSessionResponse(checkout_url=session.url)


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """
    Webhook de Stripe para escuchar eventos de suscripción.
    Configura esta URL en el Dashboard de Stripe.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=500,
            detail="No hay STRIPE_WEBHOOK_SECRET configurado.",
        )

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Firma de Webhook inválida",
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload inválido",
        )

    # ------------------------------
    # Manejar eventos importantes
    # ------------------------------
    event_type = event["type"]
    data_object = event["data"]["object"]

    # Suscripción creada / activada
    if event_type == "checkout.session.completed":
        # Aquí puedes:
        # - Marcar al usuario como suscrito en tu DB
        # - Guardar subscription_id, customer_id, etc.
        session_id = data_object.get("id")
        customer_email = data_object.get("customer_details", {}).get("email")
        subscription_id = data_object.get("subscription")
        print(
            f"[Stripe] Checkout completado: session={session_id}, "
            f"email={customer_email}, sub={subscription_id}"
        )

    elif event_type == "customer.subscription.deleted":
        # Manejar cancelación de suscripción
        subscription_id = data_object.get("id")
        print(f"[Stripe] Suscripción cancelada: {subscription_id}")

    elif event_type == "invoice.payment_failed":
        # Manejar pago fallido (podrías suspender acceso)
        subscription_id = data_object.get("subscription")
        print(f"[Stripe] Pago de suscripción fallido: {subscription_id}")

    # Puedes loguear otros eventos para debug
    else:
        print(f"[Stripe] Evento no manejado: {event_type}")

    return {"status": "ok"}

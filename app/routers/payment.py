import hmac
import hashlib
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.database import get_db
from app.models.user import User
from app.models.subscription import Subscription
from app.models.payment import Payment
from app.routers.auth import get_current_user
from app.services.razorpay_client import client as rzp_client, PLAN_CONFIG, TOPUP_PACKS
from app.config import settings

router = APIRouter(prefix="/api/payment", tags=["payment"])


# ---------- Schemas ----------

class CreateSubscriptionRequest(BaseModel):
    plan: str  # "starter" or "pro"

class CreateTopupRequest(BaseModel):
    pack_id: str  # "pack_5", "pack_15", "pack_30"

class PlanResponse(BaseModel):
    plan: str
    credits_remaining: int
    is_super_admin: bool
    subscription_status: str | None = None
    subscription_end: str | None = None


# ---------- Endpoints ----------

@router.get("/plan", response_model=PlanResponse)
async def get_user_plan(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current user's plan, credits, and subscription status."""
    sub_status = None
    sub_end = None

    result = await db.execute(
        select(Subscription)
        .where(Subscription.user_id == current_user.id, Subscription.status == "active")
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    active_sub = result.scalar_one_or_none()
    if active_sub:
        sub_status = active_sub.status
        sub_end = active_sub.current_period_end.isoformat() if active_sub.current_period_end else None

    return PlanResponse(
        plan=current_user.plan,
        credits_remaining=current_user.credits_remaining,
        is_super_admin=current_user.is_super_admin,
        subscription_status=sub_status,
        subscription_end=sub_end,
    )


@router.post("/create-order")
async def create_order(
    req: CreateSubscriptionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a Razorpay order for a subscription plan."""
    if req.plan not in PLAN_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid plan")

    config = PLAN_CONFIG[req.plan]

    try:
        order = rzp_client.order.create({
            "amount": config["amount"],
            "currency": config["currency"],
            "payment_capture": 1,
            "notes": {
                "user_id": current_user.id,
                "plan": req.plan,
                "type": "subscription",
            },
        })
    except Exception as e:
        logger.error(f"Razorpay order creation failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to create payment order")

    # Save pending payment record
    payment = Payment(
        user_id=current_user.id,
        razorpay_order_id=order["id"],
        amount=config["amount"],
        currency=config["currency"],
        payment_type="subscription",
        credits_added=config["credits_per_month"],
        status="pending",
    )
    db.add(payment)
    await db.commit()

    return {
        "order_id": order["id"],
        "amount": config["amount"],
        "currency": config["currency"],
        "key_id": settings.razorpay_key_id,
        "plan": req.plan,
    }


@router.post("/create-topup")
async def create_topup(
    req: CreateTopupRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a Razorpay order for a top-up pack."""
    if req.pack_id not in TOPUP_PACKS:
        raise HTTPException(status_code=400, detail="Invalid pack")

    pack = TOPUP_PACKS[req.pack_id]

    try:
        order = rzp_client.order.create({
            "amount": pack["amount"],
            "currency": pack["currency"],
            "payment_capture": 1,
            "notes": {
                "user_id": current_user.id,
                "pack_id": req.pack_id,
                "type": "topup",
            },
        })
    except Exception as e:
        logger.error(f"Razorpay topup order creation failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to create payment order")

    # Save pending payment record
    payment = Payment(
        user_id=current_user.id,
        razorpay_order_id=order["id"],
        amount=pack["amount"],
        currency=pack["currency"],
        payment_type="topup",
        credits_added=pack["credits"],
        status="pending",
    )
    db.add(payment)
    await db.commit()

    return {
        "order_id": order["id"],
        "amount": pack["amount"],
        "currency": pack["currency"],
        "key_id": settings.razorpay_key_id,
        "pack_id": req.pack_id,
    }


@router.post("/webhook")
async def razorpay_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Handle Razorpay webhook events — single source of truth for payment confirmation."""
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    # Verify webhook signature using webhook secret
    webhook_secret = settings.razorpay_webhook_secret or settings.razorpay_key_secret
    if webhook_secret:
        expected = hmac.new(
            webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        if expected != signature:
            logger.warning("Webhook signature verification failed")
            raise HTTPException(status_code=400, detail="Invalid webhook signature")

    payload = await request.json()
    event = payload.get("event", "")
    logger.info(f"Razorpay webhook event: {event}")

    if event == "payment.captured":
        await _handle_payment_captured(payload, db)

    elif event == "payment.failed":
        await _handle_payment_failed(payload, db)

    return {"status": "ok"}


async def _handle_payment_captured(payload: dict, db: AsyncSession):
    """Process a successful payment from Razorpay webhook."""
    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    razorpay_payment_id = payment_entity.get("id")
    razorpay_order_id = payment_entity.get("order_id")
    notes = payment_entity.get("notes", {})

    logger.info(f"Payment captured: {razorpay_payment_id}, order: {razorpay_order_id}, notes: {notes}")

    if not razorpay_order_id:
        logger.warning("Webhook payment.captured missing order_id")
        return

    # Find the pending payment record we created when the order was made
    result = await db.execute(
        select(Payment).where(Payment.razorpay_order_id == razorpay_order_id)
    )
    payment = result.scalar_one_or_none()
    if not payment:
        logger.warning(f"No pending payment found for order {razorpay_order_id}")
        return

    # Already processed — skip duplicate webhook
    if payment.status == "success":
        logger.info(f"Payment {razorpay_order_id} already processed, skipping")
        return

    # Update payment record
    payment.razorpay_payment_id = razorpay_payment_id
    payment.status = "success"
    db.add(payment)

    # Find the user
    result = await db.execute(select(User).where(User.id == payment.user_id))
    user = result.scalar_one_or_none()
    if not user:
        logger.error(f"User {payment.user_id} not found for payment {razorpay_order_id}")
        return

    payment_type = notes.get("type") or payment.payment_type

    if payment_type == "subscription":
        plan = notes.get("plan", "starter")
        if plan not in PLAN_CONFIG:
            logger.error(f"Invalid plan in webhook notes: {plan}")
            return

        config = PLAN_CONFIG[plan]

        # Create subscription record
        now = datetime.utcnow()
        subscription = Subscription(
            user_id=user.id,
            plan=plan,
            razorpay_subscription_id=razorpay_payment_id,
            status="active",
            credits_per_month=config["credits_per_month"],
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
        )
        db.add(subscription)

        # Update user plan and credits
        user.plan = plan
        user.credits_remaining = config["credits_per_month"]
        db.add(user)

        await db.commit()
        logger.info(f"User {user.id} subscribed to {plan} via webhook, credits: {config['credits_per_month']}")

    elif payment_type == "topup":
        pack_id = notes.get("pack_id")
        if pack_id and pack_id in TOPUP_PACKS:
            credits = TOPUP_PACKS[pack_id]["credits"]
        else:
            # Fallback to credits_added from payment record
            credits = payment.credits_added

        user.credits_remaining += credits
        db.add(user)

        await db.commit()
        logger.info(f"User {user.id} topup +{credits} credits via webhook, total: {user.credits_remaining}")

    else:
        logger.warning(f"Unknown payment type: {payment_type}")


async def _handle_payment_failed(payload: dict, db: AsyncSession):
    """Mark a failed payment."""
    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    razorpay_order_id = payment_entity.get("order_id")

    if not razorpay_order_id:
        return

    result = await db.execute(
        select(Payment).where(Payment.razorpay_order_id == razorpay_order_id)
    )
    payment = result.scalar_one_or_none()
    if payment and payment.status == "pending":
        payment.razorpay_payment_id = payment_entity.get("id")
        payment.status = "failed"
        db.add(payment)
        await db.commit()

    logger.warning(f"Payment failed for order {razorpay_order_id}")

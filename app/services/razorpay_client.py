import razorpay
from app.config import settings

client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))

# Plan configs (amounts in paise — 100 paise = ₹1)
PLAN_CONFIG = {
    "starter": {
        "name": "Starter",
        "amount": 84900,  # ₹849/month (~$9.99)
        "currency": "INR",
        "credits_per_month": 15,
    },
    "pro": {
        "name": "Pro",
        "amount": 209900,  # ₹2,099/month (~$24.99)
        "currency": "INR",
        "credits_per_month": 40,
    },
}

TOPUP_PACKS = {
    "pack_5": {
        "name": "5 Credits",
        "amount": 41900,  # ₹419 (~$4.99)
        "currency": "INR",
        "credits": 5,
    },
    "pack_15": {
        "name": "15 Credits",
        "amount": 109900,  # ₹1,099 (~$12.99)
        "currency": "INR",
        "credits": 15,
    },
    "pack_30": {
        "name": "30 Credits",
        "amount": 189900,  # ₹1,899 (~$22.99)
        "currency": "INR",
        "credits": 30,
    },
}

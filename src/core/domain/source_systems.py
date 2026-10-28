PAYPAL_SOURCE_SYSTEM = "paypal"
RAZORPAY_SOURCE_SYSTEM = "razorpay"
SYNTHETIC_SOURCE_SYSTEM = "synthetic"

# The only providers a Stage 5-8 payment attempt may belong to; must match the payment_attempts
# registry CHECK constraint (enforced by a test).
PIPELINE_PROVIDERS = (RAZORPAY_SOURCE_SYSTEM, SYNTHETIC_SOURCE_SYSTEM, PAYPAL_SOURCE_SYSTEM)

# Event sources excluded from Stages 2-4 (until provider-partitioned analytics are implemented) and
# from every non-PayPal attempt in Stages 5-8. PayPal attempts are resolved separately, from PayPal
# rows only (see payment_attempts.py).
NON_RAZORPAY_PIPELINE_SOURCES = (PAYPAL_SOURCE_SYSTEM,)

PAYPAL_SOURCE_SYSTEM = "paypal"

# Event sources excluded from Stages 2-4 (until provider-partitioned analytics are implemented) and
# from every non-PayPal attempt in Stages 5-8. PayPal attempts are resolved separately, from PayPal
# rows only (see payment_attempts.py).
NON_RAZORPAY_PIPELINE_SOURCES = (PAYPAL_SOURCE_SYSTEM,)

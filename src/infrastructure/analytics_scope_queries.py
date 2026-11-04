"""payment_events predicates for Stage 2-4 analytics scopes (see src/core/domain/analytics_scopes.py)."""
from src.core.domain.analytics_scopes import PAYPAL_SCOPE, validate_scope
from src.core.domain.source_systems import NON_RAZORPAY_PIPELINE_SOURCES, PAYPAL_SOURCE_SYSTEM
from src.infrastructure.models import PaymentEventModel


def scope_event_filter(provider_scope: str):
    """The events a scope analyses: PayPal rows only, or the original Razorpay/synthetic population."""
    if validate_scope(provider_scope) == PAYPAL_SCOPE:
        return PaymentEventModel.source_system == PAYPAL_SOURCE_SYSTEM
    return PaymentEventModel.source_system.notin_(NON_RAZORPAY_PIPELINE_SOURCES)

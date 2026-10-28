"""
Domain exceptions for Stage 5-8 payment attempt identity and registration.
"""


class PaymentAttemptError(Exception):
    """Base exception for payment attempt identity errors."""


class UnsupportedProviderError(PaymentAttemptError):
    """Raised when an attempt would belong to a provider outside PIPELINE_PROVIDERS."""


class InvalidAttemptIdentityError(PaymentAttemptError):
    """Raised when an attempt key, provider reference or trigger event is not a valid attempt identity."""


class AttemptNotRegisteredError(PaymentAttemptError):
    """Raised when a Stage 5-8 write refers to an attempt missing from the payment_attempts registry."""


class AttemptProviderConflictError(PaymentAttemptError):
    """Raised when an attempt is registered, or its records are stored, under a different provider."""


class ProviderIdentityMissingError(PaymentAttemptError):
    """Raised when a stored Stage 5-8 record has no source_system."""

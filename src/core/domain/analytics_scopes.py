"""
Stage 2-4 analytics partitions (provider scopes).

Health snapshots, degradation signals, episodes and RCA evidence are computed separately per scope, so
one provider's traffic can never shift another provider's baselines or trigger its episodes.

- RAZORPAY_PIPELINE_SCOPE: Razorpay and synthetic events, exactly as Stages 2-4 have always run (synthetic
  traffic stands in for Razorpay demo traffic). Every row written before scopes existed belongs here, and
  this scope's segment dimensions, RCA dimensions and episode identities are unchanged.
- PAYPAL_SCOPE: PayPal events only, with their own baselines, signals, episodes and RCA.

PayPal canonical events carry currency and, for declined captures, PayPal's decline reason
(status_details.reason, stored as error_reason); they carry no payment method, bank, wallet or error
source. So the PayPal scope segments by GLOBAL and currency, diagnoses structurally by CURRENCY, and uses
the decline reason as supporting failure evidence. A structural candidate that covers the whole episode
population (for example the only currency PayPal saw) cannot distinguish a cause, so the PayPal scope
never reports one as a root cause.
"""
from dataclasses import dataclass
from typing import Tuple

from src.core.domain.rca_models import STRUCTURAL_DIMENSIONS, SUPPORTING_DIMENSIONS
from src.core.domain.source_systems import PAYPAL_SOURCE_SYSTEM

RAZORPAY_PIPELINE_SCOPE = "razorpay_pipeline"
PAYPAL_SCOPE = "paypal"
ANALYTICS_SCOPES = (RAZORPAY_PIPELINE_SCOPE, PAYPAL_SCOPE)

# Stage 2 segment dimensions of the original pipeline (unchanged).
RAZORPAY_PIPELINE_DIMENSIONS = ("GLOBAL", "currency", "payment_method", "bank", "wallet", "error_source")


@dataclass(frozen=True)
class ScopeConfig:
    stage2_dimensions: Tuple[str, ...]
    rca_structural_dimensions: Tuple[str, ...]
    rca_supporting_dimensions: Tuple[str, ...]
    exclude_population_wide_candidates: bool


SCOPE_CONFIG = {
    RAZORPAY_PIPELINE_SCOPE: ScopeConfig(
        stage2_dimensions=RAZORPAY_PIPELINE_DIMENSIONS,
        rca_structural_dimensions=STRUCTURAL_DIMENSIONS,
        rca_supporting_dimensions=SUPPORTING_DIMENSIONS,
        exclude_population_wide_candidates=False,
    ),
    PAYPAL_SCOPE: ScopeConfig(
        stage2_dimensions=("GLOBAL", "currency"),
        rca_structural_dimensions=("CURRENCY",),
        rca_supporting_dimensions=("ERROR_REASON",),
        exclude_population_wide_candidates=True,
    ),
}


def scope_for_source(source_system: str) -> str:
    """The analytics scope an event's provider belongs to."""
    return PAYPAL_SCOPE if source_system == PAYPAL_SOURCE_SYSTEM else RAZORPAY_PIPELINE_SCOPE


def validate_scope(provider_scope: str) -> str:
    if provider_scope not in SCOPE_CONFIG:
        raise ValueError(f"Unknown analytics scope {provider_scope!r}; expected one of {ANALYTICS_SCOPES}")
    return provider_scope


def episode_identity_name(provider_scope: str, segment_dimension: str, segment_value: str, start_iso: str) -> str:
    """
    The UUID5 name of an episode. The original scope keeps its historical names, so its episode ids are
    unchanged; other scopes are prefixed, so equal segments in two scopes never share an episode.
    """
    base = f"{segment_dimension}:{segment_value}:{start_iso}"
    if provider_scope == RAZORPAY_PIPELINE_SCOPE:
        return base
    return f"{validate_scope(provider_scope)}|{base}"

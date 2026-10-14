"""
Structural parsing of PayPal REST webhook events (resource_version 2.0).

Pure functions only: no HTTP, no signature verification, no logging.
Signature verification must happen on the raw request body before this parser runs.
Error messages report field locations only, never input values, so payload data
cannot leak into logs through exceptions.
"""
import re
from dataclasses import dataclass
from typing import Annotated, Any, Dict, FrozenSet, List, Mapping, Optional, Type, Union
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from src.core.domain.exceptions import (
    MalformedInputError,
    SchemaValidationError,
    UnsupportedEventTypeError,
)

Identifier = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
EventType = Annotated[StrictStr, Field(pattern=r"^[A-Z0-9][A-Z0-9_.-]{0,127}$")]

SUPPORTED_RESOURCE_VERSION = "2.0"

# Hosts PayPal uses in HATEOAS links for live and sandbox environments.
PAYPAL_API_HOSTS = frozenset({
    "api.paypal.com",
    "api-m.paypal.com",
    "api.sandbox.paypal.com",
    "api-m.sandbox.paypal.com",
})
_CAPTURE_PATH = re.compile(r"^/v2/payments/captures/([A-Za-z0-9][A-Za-z0-9_-]{0,127})$")


class _PayPalModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class PayPalMoney(_PayPalModel):
    currency_code: StrictStr
    value: StrictStr


class PayPalRelatedIds(_PayPalModel):
    order_id: Optional[Identifier] = None


class PayPalSupplementaryData(_PayPalModel):
    related_ids: Optional[PayPalRelatedIds] = None


class PayPalLink(_PayPalModel):
    href: StrictStr
    rel: StrictStr


class PayPalStatusDetails(_PayPalModel):
    reason: Optional[StrictStr] = None


class PayPalCaptureResource(_PayPalModel):
    id: Identifier
    status: StrictStr
    amount: PayPalMoney
    status_details: Optional[PayPalStatusDetails] = None
    supplementary_data: Optional[PayPalSupplementaryData] = None


class PayPalRefundResource(_PayPalModel):
    id: Identifier
    status: StrictStr
    amount: PayPalMoney
    links: List[PayPalLink]
    supplementary_data: Optional[PayPalSupplementaryData] = None


class PayPalPurchaseUnit(_PayPalModel):
    amount: PayPalMoney
    reference_id: Optional[StrictStr] = None


class PayPalOrderResource(_PayPalModel):
    id: Identifier
    status: StrictStr
    purchase_units: Annotated[List[PayPalPurchaseUnit], Field(min_length=1)]


PayPalResource = Union[PayPalCaptureResource, PayPalRefundResource, PayPalOrderResource]


class _PayPalEventHeader(_PayPalModel):
    id: Identifier
    event_type: EventType


class _PayPalEnvelope(_PayPalEventHeader):
    create_time: StrictStr
    resource_type: StrictStr
    resource_version: StrictStr
    resource: Dict[str, Any]


@dataclass(frozen=True)
class _EventSpec:
    resource_type: str
    resource_model: Type[_PayPalModel]
    allowed_statuses: FrozenSet[str]


_EVENT_SPECS: Mapping[str, _EventSpec] = {
    "PAYMENT.CAPTURE.COMPLETED": _EventSpec("capture", PayPalCaptureResource, frozenset({"COMPLETED"})),
    "PAYMENT.CAPTURE.DECLINED": _EventSpec("capture", PayPalCaptureResource, frozenset({"DECLINED", "FAILED"})),
    "PAYMENT.CAPTURE.PENDING": _EventSpec("capture", PayPalCaptureResource, frozenset({"PENDING"})),
    "PAYMENT.CAPTURE.REFUNDED": _EventSpec("refund", PayPalRefundResource, frozenset({"COMPLETED"})),
    "CHECKOUT.ORDER.APPROVED": _EventSpec("checkout-order", PayPalOrderResource, frozenset({"APPROVED"})),
    "CHECKOUT.ORDER.COMPLETED": _EventSpec("checkout-order", PayPalOrderResource, frozenset({"COMPLETED"})),
}

SUPPORTED_EVENT_TYPES: FrozenSet[str] = frozenset(_EVENT_SPECS)


@dataclass(frozen=True)
class PayPalWebhookEvent:
    """
    A structurally validated PayPal webhook event.

    capture_id: capture events -> resource.id; refunds -> the refunded capture
                (from the refund's "up" link); orders -> None.
    order_id:   orders -> resource.id; captures/refunds -> related order id when present.
    """
    event_id: str
    event_type: str
    create_time: str
    resource_type: str
    resource: PayPalResource
    capture_id: Optional[str]
    order_id: Optional[str]


def _describe(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
        for err in error.errors()
    )


def _validate(model: Type[_PayPalModel], data: Any, context: str) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise SchemaValidationError(f"Invalid PayPal {context}: {_describe(e)}") from None


def _refunded_capture_id(refund: PayPalRefundResource) -> str:
    capture_ids = []
    for link in refund.links:
        if link.rel != "up":
            continue
        parsed = urlparse(link.href)
        match = _CAPTURE_PATH.match(parsed.path)
        if parsed.scheme == "https" and parsed.hostname in PAYPAL_API_HOSTS and match:
            capture_ids.append(match.group(1))
    if len(capture_ids) != 1:
        raise SchemaValidationError(
            "Invalid PayPal refund resource: expected exactly one 'up' link to a PayPal capture."
        )
    return capture_ids[0]


def _related_order_id(resource: Union[PayPalCaptureResource, PayPalRefundResource]) -> Optional[str]:
    related = resource.supplementary_data.related_ids if resource.supplementary_data else None
    return related.order_id if related else None


def parse_paypal_event(event: Mapping[str, Any]) -> PayPalWebhookEvent:
    """
    Validates a decoded PayPal webhook event and its resource for a supported event type.

    Raises MalformedInputError if the input is not a JSON object.
    Raises UnsupportedEventTypeError (a SchemaValidationError) for well-formed but
    unsupported event types.
    Raises SchemaValidationError for missing or invalid fields, a resource that does not
    match the event type, or missing payment identifiers.
    """
    if not isinstance(event, Mapping):
        raise MalformedInputError("PayPal webhook event must be a JSON object.")

    header = _validate(_PayPalEventHeader, event, "event envelope")
    spec = _EVENT_SPECS.get(header.event_type)
    if spec is None:
        raise UnsupportedEventTypeError(f"Unsupported PayPal event type: {header.event_type}")

    envelope = _validate(_PayPalEnvelope, event, "event envelope")
    if envelope.resource_type != spec.resource_type:
        raise SchemaValidationError(
            f"Invalid PayPal event: {header.event_type} requires resource_type "
            f"'{spec.resource_type}'."
        )
    if envelope.resource_version != SUPPORTED_RESOURCE_VERSION:
        raise SchemaValidationError(
            f"Unsupported PayPal resource_version; expected '{SUPPORTED_RESOURCE_VERSION}'."
        )

    resource = _validate(spec.resource_model, envelope.resource, f"{spec.resource_type} resource")
    if resource.status not in spec.allowed_statuses:
        raise SchemaValidationError(
            f"Invalid PayPal event: resource status is inconsistent with {header.event_type}."
        )

    if isinstance(resource, PayPalOrderResource):
        capture_id, order_id = None, resource.id
    elif isinstance(resource, PayPalRefundResource):
        capture_id, order_id = _refunded_capture_id(resource), _related_order_id(resource)
    else:
        capture_id, order_id = resource.id, _related_order_id(resource)

    return PayPalWebhookEvent(
        event_id=envelope.id,
        event_type=envelope.event_type,
        create_time=envelope.create_time,
        resource_type=envelope.resource_type,
        resource=resource,
        capture_id=capture_id,
        order_id=order_id,
    )

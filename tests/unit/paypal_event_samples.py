"""
Minimal PayPal Sandbox-style webhook events (resource_version 2.0) for unit tests.
The capture sample is trimmed from PayPal's documented PAYMENT.CAPTURE.COMPLETED event.
Each builder returns a fresh dict that tests may mutate.
"""
EVENT_ID = "WH-3F562076HD293871E-75F399086E414290U"
CAPTURE_ID = "3Y662965014333303"
ORDER_ID = "9P99943869582473S"
REFUND_ID = "1Y107995YT783435V"
CREATE_TIME = "2024-05-16T05:19:19.355Z"

SANDBOX = "https://api.sandbox.paypal.com"


def _envelope(event_type, resource_type, resource, event_id=EVENT_ID):
    return {
        "id": event_id,
        "event_version": "1.0",
        "create_time": CREATE_TIME,
        "resource_type": resource_type,
        "resource_version": "2.0",
        "event_type": event_type,
        "summary": "Sandbox event",
        "resource": resource,
        "links": [
            {"href": f"{SANDBOX}/v1/notifications/webhooks-events/{event_id}", "rel": "self", "method": "GET"},
        ],
    }


def capture_resource(status="COMPLETED", value="500.00", currency="USD"):
    return {
        "id": CAPTURE_ID,
        "status": status,
        "amount": {"value": value, "currency_code": currency},
        "final_capture": True,
        "supplementary_data": {"related_ids": {"order_id": ORDER_ID}},
        "payee": {"email_address": "merchant@example.com", "merchant_id": "QDGTZ7B92B9QT"},
        "create_time": "2024-05-16T05:19:15Z",
        "update_time": "2024-05-16T05:19:15Z",
        "links": [
            {"href": f"{SANDBOX}/v2/payments/captures/{CAPTURE_ID}", "rel": "self", "method": "GET"},
            {"href": f"{SANDBOX}/v2/checkout/orders/{ORDER_ID}", "rel": "up", "method": "GET"},
        ],
    }


def capture_completed(**resource_overrides):
    return _envelope("PAYMENT.CAPTURE.COMPLETED", "capture", capture_resource(**resource_overrides))


def capture_declined():
    resource = capture_resource(status="DECLINED")
    resource["status_details"] = {"reason": "DECLINED_BY_RISK_FRAUD_FILTERS"}
    return _envelope("PAYMENT.CAPTURE.DECLINED", "capture", resource)


def capture_pending():
    resource = capture_resource(status="PENDING")
    resource["status_details"] = {"reason": "PENDING_REVIEW"}
    return _envelope("PAYMENT.CAPTURE.PENDING", "capture", resource)


def capture_refunded(value="100.00", currency="USD"):
    resource = {
        "id": REFUND_ID,
        "status": "COMPLETED",
        "amount": {"value": value, "currency_code": currency},
        "create_time": "2024-05-17T08:00:00Z",
        "update_time": "2024-05-17T08:00:00Z",
        "links": [
            {"href": f"{SANDBOX}/v2/payments/refunds/{REFUND_ID}", "rel": "self", "method": "GET"},
            {"href": f"{SANDBOX}/v2/payments/captures/{CAPTURE_ID}", "rel": "up", "method": "GET"},
        ],
    }
    return _envelope("PAYMENT.CAPTURE.REFUNDED", "refund", resource)


def order_resource(status, value="500.00", currency="USD"):
    return {
        "id": ORDER_ID,
        "intent": "CAPTURE",
        "status": status,
        "purchase_units": [
            {"reference_id": "default", "amount": {"value": value, "currency_code": currency}},
        ],
        "payer": {"email_address": "buyer@example.com", "payer_id": "QYR5Z8XDVJNXQ"},
        "create_time": "2024-05-16T05:18:00Z",
        "links": [
            {"href": f"{SANDBOX}/v2/checkout/orders/{ORDER_ID}", "rel": "self", "method": "GET"},
        ],
    }


def order_approved(**resource_overrides):
    return _envelope("CHECKOUT.ORDER.APPROVED", "checkout-order", order_resource("APPROVED", **resource_overrides))


def order_completed():
    resource = order_resource("COMPLETED")
    resource["purchase_units"][0]["payments"] = {
        "captures": [{"id": CAPTURE_ID, "status": "COMPLETED", "amount": {"value": "500.00", "currency_code": "USD"}}]
    }
    return _envelope("CHECKOUT.ORDER.COMPLETED", "checkout-order", resource)


ALL_SUPPORTED = {
    "PAYMENT.CAPTURE.COMPLETED": capture_completed,
    "PAYMENT.CAPTURE.DECLINED": capture_declined,
    "PAYMENT.CAPTURE.PENDING": capture_pending,
    "PAYMENT.CAPTURE.REFUNDED": capture_refunded,
    "CHECKOUT.ORDER.APPROVED": order_approved,
    "CHECKOUT.ORDER.COMPLETED": order_completed,
}

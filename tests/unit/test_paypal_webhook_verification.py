import json
import logging

import httpx
import pytest

from src.core.domain.exceptions import InvalidWebhookSignatureError, MalformedInputError
from src.infrastructure.paypal_client import (
    LIVE_BASE_URL,
    SANDBOX_BASE_URL,
    PayPalAuthError,
    PayPalClient,
    PayPalConfig,
    PayPalConfigurationError,
    PayPalEnvironment,
    PayPalUnavailableError,
    PayPalVerificationRequestRejectedError,
    PayPalWebhookVerification,
)
from tests.unit import paypal_event_samples as samples

CLIENT_ID = "AbCdEf-client_id-123"
CLIENT_SECRET = "EXAMPLE-client-secret-value-XYZ"
ACCESS_TOKEN = "A21AAExampleAccessTokenValue-123"
SECOND_TOKEN = "A21AASecondAccessTokenValue-456"
WEBHOOK_ID = "8PT597110X687430LKGECATA"
TOKEN_URL = f"{SANDBOX_BASE_URL}/v1/oauth2/token"
VERIFY_URL = f"{SANDBOX_BASE_URL}/v1/notifications/verify-webhook-signature"

TRANSMISSION_SIG = (
    "lmI95Jx3Y9nhR5SJWlHVIWpg4AgFk7n9bCHSRxbrd8A9zrhdu2rMyFrmz+Zjh3s3boXB07VXCXUZy/UFzUlnGJn0"
    "wDugt7FlSvdKeIJenLRemUxYCPVoEZzg9VFNqOa48gMkvF+XTpxBeUx/kWy6B5cp7GkT2+pOowfRK7Oaynux=="
)
HEADERS = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-360caa42-fca2a594-a5cafa77",
    "PAYPAL-TRANSMISSION-ID": "69cd13f0-d67a-11e5-baa3-778b53f4ae55",
    "PAYPAL-TRANSMISSION-SIG": TRANSMISSION_SIG,
    "PAYPAL-TRANSMISSION-TIME": "2016-02-18T20:01:35Z",
}
EXPECTED_TRANSMISSION_FIELDS = {
    "auth_algo": HEADERS["PAYPAL-AUTH-ALGO"],
    "cert_url": HEADERS["PAYPAL-CERT-URL"],
    "transmission_id": HEADERS["PAYPAL-TRANSMISSION-ID"],
    "transmission_sig": TRANSMISSION_SIG,
    "transmission_time": HEADERS["PAYPAL-TRANSMISSION-TIME"],
}

EVENT = samples.capture_completed()
# Deliberately not in json.dumps' canonical form: PayPal must receive these exact bytes.
RAW_BODY = json.dumps(EVENT, indent=2).replace('"500.00"', '"500.00" ').encode("utf-8")
SENSITIVE_VALUES = (CLIENT_SECRET, ACCESS_TOKEN, SECOND_TOKEN, TRANSMISSION_SIG, samples.EVENT_ID, samples.CAPTURE_ID)


def _config(**overrides):
    values = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "webhook_id": WEBHOOK_ID}
    values.update(overrides)
    return PayPalConfig(**values)


def _token_body(access_token=ACCESS_TOKEN):
    return {"access_token": access_token, "token_type": "Bearer", "expires_in": 32400}


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Recorder:
    """MockTransport handler that answers token requests and delegates verify requests."""

    def __init__(self, verify_response, token_response=None):
        self.requests = []
        self._verify_response = verify_response
        self._token_response = token_response or (lambda request: httpx.Response(200, json=_token_body()))

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == "/v1/oauth2/token":
            return self._token_response(request)
        return self._verify_response(request)

    @property
    def verify_requests(self):
        return [r for r in self.requests if r.url.path == "/v1/notifications/verify-webhook-signature"]


def _status(value):
    return lambda request: httpx.Response(200, json={"verification_status": value})


def _client(handler, config=None, clock=None):
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PayPalClient(config or _config(), http_client=http_client, clock=clock or FakeClock())


def _assert_redacted(text):
    for value in SENSITIVE_VALUES:
        assert value not in text


# --- verification results -------------------------------------------------------

async def test_success_is_verified_and_request_shape_is_exact():
    recorder = Recorder(_status("SUCCESS"))
    client = _client(recorder)

    result = await client.verify_webhook_signature(HEADERS, RAW_BODY)

    assert result == PayPalWebhookVerification(verified=True)
    token_request, verify_request = recorder.requests
    assert str(token_request.url) == TOKEN_URL
    assert verify_request.method == "POST"
    assert str(verify_request.url) == VERIFY_URL
    assert verify_request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert verify_request.headers["content-type"] == "application/json"
    assert verify_request.headers["accept"] == "application/json"
    assert verify_request.extensions["timeout"] == {"connect": 3.0, "read": 5.0, "write": 5.0, "pool": 2.0}

    body = json.loads(verify_request.content)
    assert body == {**EXPECTED_TRANSMISSION_FIELDS, "webhook_id": WEBHOOK_ID, "webhook_event": EVENT}
    assert verify_request.content.endswith(b',"webhook_event":' + RAW_BODY + b"}")


async def test_failure_is_not_verified():
    client = _client(Recorder(_status("FAILURE")))
    assert await client.verify_webhook_signature(HEADERS, RAW_BODY) == PayPalWebhookVerification(verified=False)


async def test_live_configuration_targets_live_host():
    recorder = Recorder(_status("SUCCESS"))
    client = _client(recorder, config=_config(environment=PayPalEnvironment.LIVE))
    await client.verify_webhook_signature(HEADERS, RAW_BODY)
    assert [str(r.url) for r in recorder.requests] == [
        f"{LIVE_BASE_URL}/v1/oauth2/token",
        f"{LIVE_BASE_URL}/v1/notifications/verify-webhook-signature",
    ]


async def test_cached_token_is_reused_for_verification():
    recorder = Recorder(_status("SUCCESS"))
    client = _client(recorder)
    await client.verify_webhook_signature(HEADERS, RAW_BODY)
    await client.verify_webhook_signature(HEADERS, RAW_BODY)
    assert [r.url.path for r in recorder.requests].count("/v1/oauth2/token") == 1


async def test_cert_url_is_forwarded_unchanged_and_never_fetched():
    headers = dict(HEADERS, **{"PAYPAL-CERT-URL": "https://certs.example.test/cert.pem"})
    recorder = Recorder(_status("FAILURE"))
    client = _client(recorder)
    await client.verify_webhook_signature(headers, RAW_BODY)
    assert {r.url.host for r in recorder.requests} == {"api-m.sandbox.paypal.com"}
    assert json.loads(recorder.verify_requests[0].content)["cert_url"] == "https://certs.example.test/cert.pem"


# --- transmission headers -------------------------------------------------------

@pytest.mark.parametrize("headers", [
    {name.lower(): value for name, value in HEADERS.items()},
    {name.title(): value for name, value in HEADERS.items()},
    httpx.Headers(HEADERS),
    dict(HEADERS, **{"Content-Type": "application/json", "User-Agent": "PayPal/AUHD-214.0-56028171"}),
])
async def test_headers_are_matched_case_insensitively(headers):
    recorder = Recorder(_status("SUCCESS"))
    client = _client(recorder)
    assert (await client.verify_webhook_signature(headers, RAW_BODY)).verified is True
    body = json.loads(recorder.verify_requests[0].content)
    assert {k: body[k] for k in EXPECTED_TRANSMISSION_FIELDS} == EXPECTED_TRANSMISSION_FIELDS


@pytest.mark.parametrize("missing", list(HEADERS))
@pytest.mark.parametrize("replacement", [None, "", "   "])
async def test_missing_or_empty_header_is_rejected_before_any_request(missing, replacement):
    headers = {k: v for k, v in HEADERS.items() if k != missing}
    if replacement is not None:
        headers[missing] = replacement
    recorder = Recorder(_status("SUCCESS"))
    client = _client(recorder)

    with pytest.raises(InvalidWebhookSignatureError) as exc_info:
        await client.verify_webhook_signature(headers, RAW_BODY)

    assert str(exc_info.value) == f"Missing PayPal transmission headers: {missing}"
    assert recorder.requests == []


async def test_all_missing_headers_are_reported_by_name():
    client = _client(Recorder(_status("SUCCESS")))
    with pytest.raises(InvalidWebhookSignatureError) as exc_info:
        await client.verify_webhook_signature({}, RAW_BODY)
    assert str(exc_info.value) == "Missing PayPal transmission headers: " + ", ".join(HEADERS)


async def test_conflicting_duplicate_headers_are_rejected_without_echoing_values():
    headers = dict(HEADERS, **{"paypal-transmission-sig": "different-signature"})
    recorder = Recorder(_status("SUCCESS"))
    client = _client(recorder)
    with pytest.raises(InvalidWebhookSignatureError) as exc_info:
        await client.verify_webhook_signature(headers, RAW_BODY)
    assert str(exc_info.value) == "Conflicting PayPal transmission header: PAYPAL-TRANSMISSION-SIG"
    assert "different-signature" not in str(exc_info.value)
    _assert_redacted(str(exc_info.value))
    assert recorder.requests == []


# --- configuration and body -----------------------------------------------------

async def test_missing_webhook_id_fails_closed_before_any_request():
    recorder = Recorder(_status("SUCCESS"))
    client = _client(recorder, config=_config(webhook_id=None))
    with pytest.raises(PayPalConfigurationError, match="webhook_id is not configured"):
        await client.verify_webhook_signature(HEADERS, RAW_BODY)
    assert recorder.requests == []


def test_config_has_no_default_webhook_id():
    env = {"PAYPAL_CLIENT_ID": CLIENT_ID, "PAYPAL_CLIENT_SECRET": CLIENT_SECRET}
    config = PayPalConfig.from_env(env)
    assert config.webhook_id is None
    assert config.environment is PayPalEnvironment.SANDBOX


@pytest.mark.parametrize("raw_body", [
    EVENT,
    json.dumps(EVENT),
    b"",
    b"not json",
    b'{"id": ',
    b"\xff\xfe{}",
    b"[]",
    b'"event"',
    b"null",
])
async def test_body_that_is_not_a_raw_json_object_is_rejected_before_any_request(raw_body):
    recorder = Recorder(_status("SUCCESS"))
    client = _client(recorder)
    with pytest.raises(MalformedInputError):
        await client.verify_webhook_signature(HEADERS, raw_body)
    assert recorder.requests == []


@pytest.mark.parametrize("raw_body", [
    b'{"amount": NaN}',
    b'{"amount": Infinity}',
    b'{"amount": -Infinity}',
    b'{"resource": {"values": [1, NaN]}}',
])
async def test_non_standard_json_constants_are_rejected_before_any_request(raw_body):
    recorder = Recorder(_status("SUCCESS"))
    client = _client(recorder)
    with pytest.raises(MalformedInputError, match="not valid JSON"):
        await client.verify_webhook_signature(HEADERS, raw_body)
    assert recorder.requests == []


async def test_deeply_nested_body_is_rejected_before_any_request():
    depth = 50_000
    recorder = Recorder(_status("SUCCESS"))
    client = _client(recorder)
    with pytest.raises(MalformedInputError, match="not valid JSON"):
        await client.verify_webhook_signature(HEADERS, b"[" * depth + b"]" * depth)
    assert recorder.requests == []


async def test_constant_names_inside_strings_are_preserved_verbatim():
    raw_body = b'{"id": "NaN", "note": "Infinity",  "amount": 1.10}'
    recorder = Recorder(_status("SUCCESS"))
    client = _client(recorder)
    assert (await client.verify_webhook_signature(HEADERS, raw_body)).verified is True
    assert recorder.verify_requests[0].content.endswith(b',"webhook_event":' + raw_body + b"}")


# --- authentication failures ----------------------------------------------------

async def test_oauth_failure_raises_auth_error_and_skips_verification():
    recorder = Recorder(_status("SUCCESS"), token_response=lambda request: httpx.Response(401))
    client = _client(recorder)
    with pytest.raises(PayPalAuthError, match="OAuth credentials"):
        await client.verify_webhook_signature(HEADERS, RAW_BODY)
    assert recorder.verify_requests == []


async def test_oauth_unavailable_raises_unavailable_and_skips_verification():
    recorder = Recorder(_status("SUCCESS"), token_response=lambda request: httpx.Response(503))
    client = _client(recorder)
    with pytest.raises(PayPalUnavailableError, match="OAuth token request failed"):
        await client.verify_webhook_signature(HEADERS, RAW_BODY)
    assert recorder.verify_requests == []


async def test_verify_401_raises_auth_error_without_retry_and_discards_token():
    responses = iter([httpx.Response(401, headers={"PayPal-Debug-Id": "f1a2b3c4d5e6f"}), httpx.Response(200, json={"verification_status": "SUCCESS"})])
    tokens = iter([ACCESS_TOKEN, SECOND_TOKEN])
    recorder = Recorder(
        lambda request: next(responses),
        token_response=lambda request: httpx.Response(200, json=_token_body(next(tokens))),
    )
    client = _client(recorder)

    with pytest.raises(PayPalAuthError) as exc_info:
        await client.verify_webhook_signature(HEADERS, RAW_BODY)
    assert str(exc_info.value) == (
        "PayPal rejected the webhook verification request credentials (HTTP 401, debug_id=f1a2b3c4d5e6f)"
    )
    assert len(recorder.requests) == 2

    assert (await client.verify_webhook_signature(HEADERS, RAW_BODY)).verified is True
    assert recorder.verify_requests[-1].headers["authorization"] == f"Bearer {SECOND_TOKEN}"


async def test_verify_403_raises_auth_error_and_keeps_token():
    recorder = Recorder(lambda request: httpx.Response(403))
    client = _client(recorder)
    with pytest.raises(PayPalAuthError):
        await client.verify_webhook_signature(HEADERS, RAW_BODY)
    assert await client.get_access_token() == ACCESS_TOKEN
    assert len(recorder.requests) == 2


# --- provider and protocol errors -----------------------------------------------

async def test_http_400_raises_rejected_request_not_unavailable():
    recorder = Recorder(lambda request: httpx.Response(
        400,
        json={"name": "VALIDATION_ERROR", "details": [TRANSMISSION_SIG, ACCESS_TOKEN, samples.EVENT_ID]},
        headers={"PayPal-Debug-Id": "f1a2b3c4d5e6f"},
    ))
    client = _client(recorder)

    with pytest.raises(PayPalVerificationRequestRejectedError) as exc_info:
        await client.verify_webhook_signature(HEADERS, RAW_BODY)

    assert not isinstance(exc_info.value, (PayPalUnavailableError, PayPalAuthError))
    assert str(exc_info.value) == (
        "PayPal rejected the webhook verification request as invalid (HTTP 400, debug_id=f1a2b3c4d5e6f)"
    )
    _assert_redacted(str(exc_info.value))
    _assert_redacted(repr(exc_info.value))
    assert len(recorder.requests) == 2
    assert await client.get_access_token() == ACCESS_TOKEN
    assert len(recorder.requests) == 2


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429, 404, 422, 204])
async def test_non_200_responses_raise_unavailable(status):
    recorder = Recorder(lambda request: httpx.Response(status, text=f"error {TRANSMISSION_SIG} {ACCESS_TOKEN}"))
    client = _client(recorder)
    with pytest.raises(PayPalUnavailableError) as exc_info:
        await client.verify_webhook_signature(HEADERS, RAW_BODY)
    assert str(exc_info.value) == f"PayPal webhook verification request failed (HTTP {status})"


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_redirects_are_not_followed(status):
    recorder = Recorder(lambda request: httpx.Response(status, headers={"Location": "https://attacker.example.test/steal"}))
    client = _client(recorder)
    with pytest.raises(PayPalUnavailableError, match=f"HTTP {status}"):
        await client.verify_webhook_signature(HEADERS, RAW_BODY)
    assert [r.url.host for r in recorder.requests] == ["api-m.sandbox.paypal.com"] * 2


@pytest.mark.parametrize("error, message", [
    (httpx.ConnectError, "failed before a response was received"),
    (httpx.ReadError, "failed before a response was received"),
    (httpx.RemoteProtocolError, "failed before a response was received"),
    (httpx.ConnectTimeout, "timed out"),
    (httpx.ReadTimeout, "timed out"),
    (httpx.WriteTimeout, "timed out"),
    (httpx.PoolTimeout, "timed out"),
])
async def test_transport_errors_and_timeouts_raise_unavailable(error, message):
    def verify(request):
        raise error(f"transport failure {TRANSMISSION_SIG} {ACCESS_TOKEN}", request=request)

    client = _client(Recorder(verify))
    with pytest.raises(PayPalUnavailableError, match=message) as exc_info:
        await client.verify_webhook_signature(HEADERS, RAW_BODY)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True
    _assert_redacted(str(exc_info.value))


@pytest.mark.parametrize("content", [b"", b"not json", b'{"verification_status": '])
async def test_invalid_json_raises_unavailable(content):
    client = _client(Recorder(lambda request: httpx.Response(200, content=content)))
    with pytest.raises(PayPalUnavailableError, match="not valid JSON"):
        await client.verify_webhook_signature(HEADERS, RAW_BODY)


@pytest.mark.parametrize("body", [
    [],
    ["SUCCESS"],
    "SUCCESS",
    True,
    {},
    {"status": "SUCCESS"},
    {"verification_status": None},
    {"verification_status": True},
    {"verification_status": 1},
    {"verification_status": ["SUCCESS"]},
])
async def test_malformed_response_raises_unavailable(body):
    client = _client(Recorder(lambda request: httpx.Response(200, json=body)))
    with pytest.raises(PayPalUnavailableError, match="malformed"):
        await client.verify_webhook_signature(HEADERS, RAW_BODY)


@pytest.mark.parametrize("value", ["success", "Success", " SUCCESS", "SUCCESS ", "PENDING", "", f"{TRANSMISSION_SIG}"])
async def test_unexpected_verification_status_raises_unavailable(value):
    client = _client(Recorder(_status(value)))
    with pytest.raises(PayPalUnavailableError) as exc_info:
        await client.verify_webhook_signature(HEADERS, RAW_BODY)
    assert str(exc_info.value) == (
        "PayPal webhook verification response has an unexpected verification_status (HTTP 200)"
    )


# --- redaction ------------------------------------------------------------------

async def test_no_secret_signature_or_payload_leaks_into_errors_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    leaky_body = {"verification_status": "UNKNOWN", "echo": [TRANSMISSION_SIG, ACCESS_TOKEN, CLIENT_SECRET]}
    scenarios = [
        Recorder(_status("SUCCESS")),
        Recorder(_status("FAILURE")),
        Recorder(lambda request: httpx.Response(200, json=leaky_body)),
        Recorder(lambda request: httpx.Response(500, json=leaky_body)),
        Recorder(lambda request: httpx.Response(400, json=leaky_body)),
        Recorder(lambda request: httpx.Response(401, json=leaky_body)),
        Recorder(lambda request: httpx.Response(200, content=json.dumps(leaky_body)[:-3].encode())),
    ]
    messages = []
    for recorder in scenarios:
        client = _client(recorder)
        try:
            result = await client.verify_webhook_signature(HEADERS, RAW_BODY)
            messages.append(repr(result))
        except Exception as exc:
            messages.append(f"{type(exc).__name__}: {exc!s} {exc!r}")
        messages.append(repr(client))

    for headers in ({}, dict(HEADERS, **{"paypal-transmission-sig": "x"})):
        try:
            await _client(Recorder(_status("SUCCESS"))).verify_webhook_signature(headers, RAW_BODY)
        except InvalidWebhookSignatureError as exc:
            messages.append(f"{exc!s} {exc!r}")

    _assert_redacted("\n".join(messages))
    _assert_redacted(caplog.text)

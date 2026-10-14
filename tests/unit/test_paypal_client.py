import asyncio
import base64
import logging

import httpx
import pytest
from pydantic import SecretStr

from src.infrastructure.paypal_client import (
    DEFAULT_TIMEOUT,
    LIVE_BASE_URL,
    SANDBOX_BASE_URL,
    PayPalAuthError,
    PayPalClient,
    PayPalConfig,
    PayPalConfigurationError,
    PayPalEnvironment,
    PayPalUnavailableError,
)

CLIENT_ID = "AbCdEf-client_id-123"
CLIENT_SECRET = "EXAMPLE-client-secret-value-XYZ"
ACCESS_TOKEN = "A21AAExampleAccessTokenValue-123"
SECOND_TOKEN = "A21AASecondAccessTokenValue-456"
TOKEN_URL = f"{SANDBOX_BASE_URL}/v1/oauth2/token"
SENSITIVE_VALUES = (CLIENT_SECRET, ACCESS_TOKEN, SECOND_TOKEN)


def _config(**overrides):
    values = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    values.update(overrides)
    return PayPalConfig(**values)


def _token_body(access_token=ACCESS_TOKEN, expires_in=32400):
    return {
        "scope": "https://uri.paypal.com/services/payments/payment",
        "access_token": access_token,
        "token_type": "Bearer",
        "app_id": "APP-80W284485P519543T",
        "expires_in": expires_in,
        "nonce": "2024-05-16T05:19:19ZexampleNonce",
    }


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _client(handler, clock=None, config=None):
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PayPalClient(config or _config(), http_client=http_client, clock=clock or FakeClock())


def _assert_redacted(text):
    for value in SENSITIVE_VALUES:
        assert value not in text
    assert base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode() not in text


# --- configuration --------------------------------------------------------------

def test_config_defaults_to_sandbox():
    config = _config()
    assert config.environment is PayPalEnvironment.SANDBOX
    assert config.base_url == SANDBOX_BASE_URL == "https://api-m.sandbox.paypal.com"


def test_from_env_defaults_to_sandbox_when_environment_is_unset_or_blank():
    for env in (
        {"PAYPAL_CLIENT_ID": CLIENT_ID, "PAYPAL_CLIENT_SECRET": CLIENT_SECRET},
        {"PAYPAL_CLIENT_ID": CLIENT_ID, "PAYPAL_CLIENT_SECRET": CLIENT_SECRET, "PAYPAL_ENVIRONMENT": "  "},
    ):
        assert PayPalConfig.from_env(env).base_url == SANDBOX_BASE_URL


def test_live_requires_explicit_configuration():
    assert _config(environment=PayPalEnvironment.LIVE).base_url == LIVE_BASE_URL
    env = {"PAYPAL_CLIENT_ID": CLIENT_ID, "PAYPAL_CLIENT_SECRET": CLIENT_SECRET, "PAYPAL_ENVIRONMENT": "live"}
    assert PayPalConfig.from_env(env).base_url == LIVE_BASE_URL


@pytest.mark.parametrize("value", ["production", "prod", "LIVE", "Sandbox", "https://api-m.paypal.com"])
def test_unknown_environment_is_rejected_instead_of_falling_back(value):
    env = {"PAYPAL_CLIENT_ID": CLIENT_ID, "PAYPAL_CLIENT_SECRET": CLIENT_SECRET, "PAYPAL_ENVIRONMENT": value}
    with pytest.raises(PayPalConfigurationError, match="PAYPAL_ENVIRONMENT"):
        PayPalConfig.from_env(env)


def test_environment_must_be_an_enum_member():
    with pytest.raises(PayPalConfigurationError):
        _config(environment="live")


@pytest.mark.parametrize("env, missing", [
    ({}, "PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET"),
    ({"PAYPAL_CLIENT_SECRET": CLIENT_SECRET}, "PAYPAL_CLIENT_ID"),
    ({"PAYPAL_CLIENT_ID": CLIENT_ID}, "PAYPAL_CLIENT_SECRET"),
    ({"PAYPAL_CLIENT_ID": CLIENT_ID, "PAYPAL_CLIENT_SECRET": "   "}, "PAYPAL_CLIENT_SECRET"),
])
def test_from_env_reports_missing_credentials_by_name(env, missing):
    with pytest.raises(PayPalConfigurationError) as exc_info:
        PayPalConfig.from_env(env)
    assert str(exc_info.value) == f"Missing PayPal configuration: {missing}"


@pytest.mark.parametrize("overrides", [
    {"client_id": ""},
    {"client_id": None},
    {"client_id": "id:with-colon"},
    {"client_id": "id with spaces"},
    {"client_secret": ""},
    {"client_secret": "   "},
    {"client_secret": None},
    {"client_secret": SecretStr("")},
    {"webhook_id": ""},
    {"webhook_id": "bad id"},
])
def test_invalid_credentials_fail_at_construction(overrides):
    with pytest.raises(PayPalConfigurationError) as exc_info:
        _config(**overrides)
    _assert_redacted(str(exc_info.value))


def test_webhook_id_is_optional_and_read_from_env():
    assert _config().webhook_id is None
    env = {"PAYPAL_CLIENT_ID": CLIENT_ID, "PAYPAL_CLIENT_SECRET": CLIENT_SECRET, "PAYPAL_WEBHOOK_ID": "8PT597110X687430LKGECATA"}
    assert PayPalConfig.from_env(env).webhook_id == "8PT597110X687430LKGECATA"


def test_client_requires_a_config():
    with pytest.raises(PayPalConfigurationError):
        PayPalClient(None)


def test_config_and_client_repr_hide_the_secret():
    config = _config()
    client = PayPalClient(config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: None)))
    _assert_redacted(repr(config))
    _assert_redacted(str(config))
    _assert_redacted(repr(client))
    assert config.client_secret.get_secret_value() == CLIENT_SECRET


# --- token acquisition ----------------------------------------------------------

async def test_successful_token_acquisition_and_request_shape():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_token_body())

    client = _client(handler)
    assert await client.get_access_token() == ACCESS_TOKEN

    [request] = requests
    assert request.method == "POST"
    assert str(request.url) == TOKEN_URL
    expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {expected_basic}"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.headers["accept"] == "application/json"
    assert request.content == b"grant_type=client_credentials"
    assert request.extensions["timeout"] == {"connect": 3.0, "read": 5.0, "write": 5.0, "pool": 2.0}


async def test_live_configuration_targets_live_host():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json=_token_body())

    client = _client(handler, config=_config(environment=PayPalEnvironment.LIVE))
    await client.get_access_token()
    assert urls == [f"{LIVE_BASE_URL}/v1/oauth2/token"]


def test_default_timeouts_are_explicit():
    assert DEFAULT_TIMEOUT.connect == 3.0
    assert DEFAULT_TIMEOUT.read == 5.0
    assert DEFAULT_TIMEOUT.write == 5.0
    assert DEFAULT_TIMEOUT.pool == 2.0


async def test_token_is_cached():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_token_body())

    clock = FakeClock()
    client = _client(handler, clock=clock)
    assert await client.get_access_token() == ACCESS_TOKEN
    clock.now += 30000
    assert await client.get_access_token() == ACCESS_TOKEN
    assert len(calls) == 1


async def test_token_is_refreshed_60_seconds_before_expiry():
    tokens = iter([ACCESS_TOKEN, SECOND_TOKEN])

    def handler(request):
        return httpx.Response(200, json=_token_body(access_token=next(tokens), expires_in=3600))

    clock = FakeClock(now=1000.0)
    client = _client(handler, clock=clock)
    assert await client.get_access_token() == ACCESS_TOKEN

    clock.now = 1000.0 + 3600 - 60 - 0.001
    assert await client.get_access_token() == ACCESS_TOKEN

    clock.now = 1000.0 + 3600 - 60
    assert await client.get_access_token() == SECOND_TOKEN


async def test_concurrent_callers_share_one_token_fetch():
    calls = []
    release = asyncio.Event()

    async def handler(request):
        calls.append(request)
        await release.wait()
        return httpx.Response(200, json=_token_body())

    client = _client(handler)
    tasks = [asyncio.create_task(client.get_access_token()) for _ in range(10)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)

    assert results == [ACCESS_TOKEN] * 10
    assert len(calls) == 1


async def test_failed_fetch_is_not_cached_and_next_call_retries():
    responses = iter([httpx.Response(503), httpx.Response(200, json=_token_body())])

    def handler(request):
        return next(responses)

    client = _client(handler)
    with pytest.raises(PayPalUnavailableError):
        await client.get_access_token()
    assert await client.get_access_token() == ACCESS_TOKEN


# --- malformed responses --------------------------------------------------------

@pytest.mark.parametrize("content", [b"not json", b"", b"{\"access_token\": "])
async def test_invalid_json_is_unavailable(content):
    client = _client(lambda request: httpx.Response(200, content=content))
    with pytest.raises(PayPalUnavailableError, match="not valid JSON"):
        await client.get_access_token()


@pytest.mark.parametrize("body", [
    [],
    "token",
    {},
    {k: v for k, v in _token_body().items() if k != "access_token"},
    dict(_token_body(), access_token=""),
    dict(_token_body(), access_token=123),
    dict(_token_body(), access_token="has space"),
    dict(_token_body(), access_token="x" * 4097),
    {k: v for k, v in _token_body().items() if k != "token_type"},
    dict(_token_body(), token_type="Basic"),
    {k: v for k, v in _token_body().items() if k != "expires_in"},
    dict(_token_body(), expires_in=0),
    dict(_token_body(), expires_in=-5),
    dict(_token_body(), expires_in="32400"),
    dict(_token_body(), expires_in=3.5),
    dict(_token_body(), expires_in=True),
])
async def test_malformed_token_response_is_unavailable(body):
    client = _client(lambda request: httpx.Response(200, json=body))
    with pytest.raises(PayPalUnavailableError, match="malformed") as exc_info:
        await client.get_access_token()
    _assert_redacted(str(exc_info.value))


@pytest.mark.parametrize("expires_in", [86401, 10**18, 10**400])
async def test_excessive_expires_in_is_unavailable_and_not_cached(expires_in):
    responses = iter([
        httpx.Response(200, json=_token_body(expires_in=expires_in)),
        httpx.Response(200, json=_token_body(access_token=SECOND_TOKEN)),
    ])
    client = _client(lambda request: next(responses))
    with pytest.raises(PayPalUnavailableError, match="malformed"):
        await client.get_access_token()
    assert await client.get_access_token() == SECOND_TOKEN


async def test_maximum_expires_in_is_accepted():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_token_body(expires_in=86400))

    clock = FakeClock(now=1000.0)
    client = _client(handler, clock=clock)
    assert await client.get_access_token() == ACCESS_TOKEN
    clock.now = 1000.0 + 86400 - 60 - 0.001
    assert await client.get_access_token() == ACCESS_TOKEN
    assert len(calls) == 1
    clock.now = 1000.0 + 86400 - 60
    await client.get_access_token()
    assert len(calls) == 2


# --- error responses ------------------------------------------------------------

@pytest.mark.parametrize("status", [400, 401, 403])
async def test_rejected_credentials_raise_auth_error(status):
    def handler(request):
        return httpx.Response(
            status,
            json={"error": "invalid_client", "error_description": f"Client Authentication failed {CLIENT_SECRET}"},
            headers={"PayPal-Debug-Id": "f1a2b3c4d5e6f"},
        )

    client = _client(handler)
    with pytest.raises(PayPalAuthError) as exc_info:
        await client.get_access_token()
    assert str(exc_info.value) == f"PayPal rejected the OAuth credentials (HTTP {status}, debug_id=f1a2b3c4d5e6f)"
    _assert_redacted(str(exc_info.value))


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429, 302])
async def test_server_errors_raise_unavailable(status):
    client = _client(lambda request: httpx.Response(status, text=f"error {ACCESS_TOKEN}"))
    with pytest.raises(PayPalUnavailableError) as exc_info:
        await client.get_access_token()
    assert str(exc_info.value) == f"PayPal OAuth token request failed (HTTP {status})"


async def test_unsafe_debug_id_is_omitted():
    client = _client(lambda request: httpx.Response(500, headers={"PayPal-Debug-Id": "bad id\r\ninjected"}))
    with pytest.raises(PayPalUnavailableError) as exc_info:
        await client.get_access_token()
    assert str(exc_info.value) == "PayPal OAuth token request failed (HTTP 500)"


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
    def handler(request):
        raise error(f"transport failure {CLIENT_SECRET}", request=request)

    client = _client(handler)
    with pytest.raises(PayPalUnavailableError, match=message) as exc_info:
        await client.get_access_token()
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True
    _assert_redacted(str(exc_info.value))


# --- redaction ------------------------------------------------------------------

async def test_no_secret_or_token_is_logged(caplog):
    responses = iter([httpx.Response(200, json=_token_body()), httpx.Response(401)])

    def handler(request):
        return next(responses)

    caplog.set_level(logging.DEBUG)
    clock = FakeClock()
    client = _client(handler, clock=clock)
    await client.get_access_token()
    clock.now += 100000
    with pytest.raises(PayPalAuthError):
        await client.get_access_token()
    _assert_redacted(caplog.text)


async def test_owned_http_client_is_closed():
    client = PayPalClient(_config())
    await client.aclose()
    assert client._http.is_closed


async def test_injected_http_client_is_not_closed():
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    async with PayPalClient(_config(), http_client=http_client):
        pass
    assert not http_client.is_closed
    await http_client.aclose()

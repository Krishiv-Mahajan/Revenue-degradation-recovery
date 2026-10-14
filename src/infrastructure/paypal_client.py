"""
PayPal REST API client: configuration and OAuth 2.0 client-credentials authentication.

Error messages carry only HTTP status information and PayPal's debug ID. Credentials,
Authorization headers, access tokens and response bodies are never logged or included in
exceptions.
"""
import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Optional

import httpx
from pydantic import SecretStr

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
LIVE_BASE_URL = "https://api-m.paypal.com"
TOKEN_PATH = "/v1/oauth2/token"

TOKEN_REFRESH_MARGIN_SECONDS = 60
MAX_TOKEN_LIFETIME_SECONDS = 86400
DEFAULT_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=2.0)

_MAX_TOKEN_LENGTH = 4096
_TOKEN_PATTERN = re.compile(r"^[\x21-\x7e]+$")
_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_WEBHOOK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_DEBUG_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_AUTH_REJECTION_STATUSES = frozenset({400, 401, 403})


class PayPalClientError(Exception):
    """Base class for PayPal client errors."""
    pass


class PayPalConfigurationError(PayPalClientError):
    """PayPal configuration is missing or invalid."""
    pass


class PayPalAuthError(PayPalClientError):
    """PayPal rejected the configured credentials."""
    pass


class PayPalUnavailableError(PayPalClientError):
    """PayPal could not be reached or returned an unusable response."""
    pass


class PayPalEnvironment(str, Enum):
    SANDBOX = "sandbox"
    LIVE = "live"


_BASE_URLS = {
    PayPalEnvironment.SANDBOX: SANDBOX_BASE_URL,
    PayPalEnvironment.LIVE: LIVE_BASE_URL,
}


@dataclass(frozen=True)
class PayPalConfig:
    """
    PayPal credentials and environment. The base URL is derived from the environment only,
    so credentials can never be sent to an arbitrary host. Live requires an explicit choice.
    """
    client_id: str
    client_secret: SecretStr
    webhook_id: Optional[str] = None
    environment: PayPalEnvironment = PayPalEnvironment.SANDBOX

    def __post_init__(self):
        if not isinstance(self.client_id, str) or not _CLIENT_ID_PATTERN.match(self.client_id):
            raise PayPalConfigurationError("PayPal client_id is missing or invalid")

        secret = self.client_secret
        if isinstance(secret, str):
            secret = SecretStr(secret)
            object.__setattr__(self, "client_secret", secret)
        if not isinstance(secret, SecretStr) or not secret.get_secret_value().strip():
            raise PayPalConfigurationError("PayPal client_secret is missing or invalid")

        if self.webhook_id is not None and (
            not isinstance(self.webhook_id, str) or not _WEBHOOK_ID_PATTERN.match(self.webhook_id)
        ):
            raise PayPalConfigurationError("PayPal webhook_id is invalid")

        if not isinstance(self.environment, PayPalEnvironment):
            raise PayPalConfigurationError("PayPal environment must be a PayPalEnvironment")

    @property
    def base_url(self) -> str:
        return _BASE_URLS[self.environment]

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "PayPalConfig":
        """
        Reads PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_WEBHOOK_ID (optional) and
        PAYPAL_ENVIRONMENT ("sandbox" by default, "live" only when set explicitly).
        """
        env = os.environ if env is None else env

        missing = [name for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET") if not env.get(name, "").strip()]
        if missing:
            raise PayPalConfigurationError(f"Missing PayPal configuration: {', '.join(missing)}")

        raw_environment = env.get("PAYPAL_ENVIRONMENT", "").strip() or PayPalEnvironment.SANDBOX.value
        try:
            environment = PayPalEnvironment(raw_environment)
        except ValueError:
            raise PayPalConfigurationError(
                "PAYPAL_ENVIRONMENT must be 'sandbox' or 'live'"
            ) from None

        webhook_id = env.get("PAYPAL_WEBHOOK_ID", "").strip() or None

        return cls(
            client_id=env["PAYPAL_CLIENT_ID"].strip(),
            client_secret=SecretStr(env["PAYPAL_CLIENT_SECRET"]),
            webhook_id=webhook_id,
            environment=environment,
        )


@dataclass(frozen=True)
class _CachedToken:
    value: str = field(repr=False)
    refresh_at: float


class PayPalClient:
    """
    Async PayPal REST client. Pass an httpx.AsyncClient to control transport (tests use
    httpx.MockTransport); otherwise the client creates and owns one.
    """

    def __init__(
        self,
        config: PayPalConfig,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(config, PayPalConfig):
            raise PayPalConfigurationError("PayPal client requires a PayPalConfig")
        self._config = config
        self._timeout = timeout
        self._clock = clock
        self._owns_http_client = http_client is None
        self._http = http_client if http_client is not None else httpx.AsyncClient(timeout=timeout)
        self._token: Optional[_CachedToken] = None
        self._token_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"PayPalClient(environment={self._config.environment.value!r})"

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http.aclose()

    async def __aenter__(self) -> "PayPalClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def get_access_token(self) -> str:
        """Returns a cached access token, fetching a new one once it is within the refresh margin."""
        token = self._valid_cached_token()
        if token is not None:
            return token

        async with self._token_lock:
            token = self._valid_cached_token()
            if token is not None:
                return token
            cached = await self._fetch_token()
            self._token = cached
            return cached.value

    def _valid_cached_token(self) -> Optional[str]:
        token = self._token
        if token is not None and self._clock() < token.refresh_at:
            return token.value
        return None

    async def _fetch_token(self) -> _CachedToken:
        requested_at = self._clock()
        try:
            response = await self._http.post(
                f"{self._config.base_url}{TOKEN_PATH}",
                data={"grant_type": "client_credentials"},
                auth=httpx.BasicAuth(
                    self._config.client_id, self._config.client_secret.get_secret_value()
                ),
                headers={"Accept": "application/json"},
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise PayPalUnavailableError("PayPal OAuth token request timed out") from None
        except httpx.HTTPError:
            raise PayPalUnavailableError("PayPal OAuth token request failed before a response was received") from None

        status_info = _status_info(response)
        if response.status_code in _AUTH_REJECTION_STATUSES:
            raise PayPalAuthError(f"PayPal rejected the OAuth credentials ({status_info})")
        if response.status_code != 200:
            raise PayPalUnavailableError(f"PayPal OAuth token request failed ({status_info})")

        try:
            body = response.json()
        except ValueError:
            raise PayPalUnavailableError(f"PayPal OAuth token response is not valid JSON ({status_info})") from None

        if not isinstance(body, dict):
            raise PayPalUnavailableError(f"PayPal OAuth token response is malformed ({status_info})")

        access_token = body.get("access_token")
        token_type = body.get("token_type")
        expires_in = body.get("expires_in")
        if (
            not isinstance(access_token, str)
            or len(access_token) > _MAX_TOKEN_LENGTH
            or not _TOKEN_PATTERN.match(access_token)
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
            or not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in <= 0
            or expires_in > MAX_TOKEN_LIFETIME_SECONDS
        ):
            raise PayPalUnavailableError(f"PayPal OAuth token response is malformed ({status_info})")

        refresh_at = requested_at + max(expires_in - TOKEN_REFRESH_MARGIN_SECONDS, 0)
        return _CachedToken(value=access_token, refresh_at=refresh_at)


def _status_info(response: httpx.Response) -> str:
    info = f"HTTP {response.status_code}"
    debug_id = response.headers.get("paypal-debug-id")
    if debug_id and _DEBUG_ID_PATTERN.match(debug_id):
        info += f", debug_id={debug_id}"
    return info

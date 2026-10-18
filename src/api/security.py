"""
Dashboard access control: HTTP Basic credentials, the API documentation toggle and trusted Host headers.
Settings are read from the environment on every request so corrected configuration takes effect.
"""
import logging
import os
import re
import secrets
from typing import List

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

DASHBOARD_USERNAME_ENV = "DASHBOARD_USERNAME"
DASHBOARD_PASSWORD_ENV = "DASHBOARD_PASSWORD"
ENABLE_API_DOCS_ENV = "ENABLE_API_DOCS"
ALLOWED_HOSTS_ENV = "ALLOWED_HOSTS"
DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1")

DASHBOARD_REALM = "Payment Recovery Dashboard"
_basic_credentials = HTTPBasic(realm=DASHBOARD_REALM)


async def require_dashboard_auth(request: Request) -> None:
    """
    Fails closed with 503 while dashboard credentials are not configured, and with 401 plus a Basic
    challenge when the request's credentials are missing, malformed or wrong. Never logs credentials.
    """
    username = os.getenv(DASHBOARD_USERNAME_ENV, "")
    password = os.getenv(DASHBOARD_PASSWORD_ENV, "")
    if not username.strip() or not password.strip():
        logger.warning(
            f"Dashboard access disabled: {DASHBOARD_USERNAME_ENV} and {DASHBOARD_PASSWORD_ENV} must both be set"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dashboard temporarily unavailable"
        )

    credentials = await _basic_credentials(request)
    # Both comparisons always run so timing does not reveal which field matched.
    username_matches = secrets.compare_digest(credentials.username.encode(), username.encode())
    password_matches = secrets.compare_digest(credentials.password.encode(), password.encode())
    if not (username_matches and password_matches):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": f'Basic realm="{DASHBOARD_REALM}"'},
        )


def api_docs_enabled() -> bool:
    return os.getenv(ENABLE_API_DOCS_ENV, "").strip().lower() in {"1", "true", "yes"}


async def require_api_docs_enabled() -> None:
    """Disabled documentation routes are indistinguishable from routes that do not exist."""
    if not api_docs_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST_NAME = rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})*"
# An exact hostname or IPv4 address, or Starlette's only supported wildcard form, "*.example.com".
_ALLOWED_HOST_ENTRY = re.compile(rf"(?:\*\.)?{_HOST_NAME}")


class InvalidAllowedHostsError(ValueError):
    """ALLOWED_HOSTS is unusable. Messages are fixed text and never include configured values."""


def _is_overly_broad_wildcard(host: str) -> bool:
    """
    "*.com" or "*.localhost" covers a whole top-level domain and "*.0.1" can only match IP addresses.
    Shared multi-label suffixes are handled by SHARED_SUFFIX_WILDCARD_DENYLIST.
    """
    if not host.startswith("*."):
        return False
    labels = host[2:].split(".")
    return len(labels) < 2 or labels[-1].isdigit()


# A wildcard over one of these suffixes matches hosts owned by unrelated parties. This is a short,
# conservative safeguard, not a public-suffix list: suffixes missing from it are not detected.
# Add an entry only for a suffix under which independent parties receive their own subdomains.
SHARED_SUFFIX_WILDCARD_DENYLIST = frozenset({
    # Public second-level registry suffixes.
    "co.uk",
    "org.uk",
    "com.au",
    "co.in",
    # Multi-tenant tunnel services: every user's tunnel is a subdomain of the same suffix.
    "ngrok-free.app",
    "ngrok-free.dev",
    "ngrok.app",
    "ngrok.io",
    "trycloudflare.com",
    "loca.lt",
})


def _is_shared_suffix_wildcard(host: str) -> bool:
    return host.startswith("*.") and host[2:].lower() in SHARED_SUFFIX_WILDCARD_DENYLIST


def get_allowed_hosts() -> List[str]:
    configured = [host.strip() for host in os.getenv(ALLOWED_HOSTS_ENV, "").split(",") if host.strip()]
    if not configured:
        return list(DEFAULT_ALLOWED_HOSTS)
    if "*" in configured:
        raise InvalidAllowedHostsError(f"{ALLOWED_HOSTS_ENV} must not allow every host")
    if not all(_ALLOWED_HOST_ENTRY.fullmatch(host) for host in configured):
        raise InvalidAllowedHostsError(f"{ALLOWED_HOSTS_ENV} contains a malformed entry")
    if any(_is_overly_broad_wildcard(host) for host in configured):
        raise InvalidAllowedHostsError(f"{ALLOWED_HOSTS_ENV} contains an overly broad wildcard entry")
    if any(_is_shared_suffix_wildcard(host) for host in configured):
        raise InvalidAllowedHostsError(f"{ALLOWED_HOSTS_ENV} contains a shared-suffix wildcard entry")
    return configured


class ConfiguredTrustedHostMiddleware:
    """
    Starlette's TrustedHostMiddleware using ALLOWED_HOSTS as configured at request time.
    An unusable configuration fails closed with 503 instead of disabling or crashing host validation.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        try:
            allowed_hosts = get_allowed_hosts()
        except InvalidAllowedHostsError as exc:
            logger.warning(f"Host validation failed closed: {exc}")
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1011})
                return
            response = JSONResponse({"detail": "Service temporarily unavailable"}, status_code=503)
            await response(scope, receive, send)
            return
        await TrustedHostMiddleware(self.app, allowed_hosts=allowed_hosts)(scope, receive, send)


class AuthenticatedStaticFiles(StaticFiles):
    """Static files served only to requests that pass require_dashboard_auth."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await require_dashboard_auth(Request(scope, receive))
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
            await response(scope, receive, send)
            return
        await super().__call__(scope, receive, send)

"""
Offline tests for dashboard authentication, API documentation exposure and trusted Host headers.
A fake session stands in for the database; no test touches PostgreSQL or the network.
"""
import base64
import logging

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

from src.api.dependencies import RAZORPAY_WEBHOOK_SECRET_ENV, get_ingestion_service
from starlette.websockets import WebSocketDisconnect

from src.api.security import (
    ALLOWED_HOSTS_ENV,
    DASHBOARD_PASSWORD_ENV,
    DASHBOARD_USERNAME_ENV,
    DEFAULT_ALLOWED_HOSTS,
    ENABLE_API_DOCS_ENV,
    SHARED_SUFFIX_WILDCARD_DENYLIST,
    AuthenticatedStaticFiles,
    InvalidAllowedHostsError,
    get_allowed_hosts,
    require_dashboard_auth,
)
from src.infrastructure.database import get_db_session
from src.main import app

USERNAME = "dashboard-test-user"
PASSWORD = "dashboard-test-password-7f3a9c"
CHALLENGE = 'Basic realm="Payment Recovery Dashboard"'
ENCODED_CREDENTIALS = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()

PROTECTED_PATHS = [
    "/",
    "/api/v1/summary",
    "/api/v1/episodes",
    "/api/v1/rca/00000000-0000-0000-0000-000000000000",
    "/api/v1/attributions",
    "/api/v1/recovery",
    "/api/v1/payments",
    "/api/v1/timeline/pay_dashboard_test",
    "/static/index.html",
    "/static/js/app.js",
    "/static/js/api.js",
    "/static/css/app.css",
]
PUBLIC_ROUTE_PATHS = {"/health", "/ingest/razorpay", "/webhooks/paypal"}
DOCS_PATHS = ["/docs", "/redoc", "/openapi.json"]


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []


class FakeSession:
    async def execute(self, statement):
        return _EmptyResult()


@pytest.fixture
def sessions():
    """Replaces the database session with a fake; yields every session opened."""
    previous = dict(app.dependency_overrides)
    opened = []

    async def fake_db_session():
        session = FakeSession()
        opened.append(session)
        yield session

    app.dependency_overrides[get_db_session] = fake_db_session
    yield opened
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(DASHBOARD_USERNAME_ENV, USERNAME)
    monkeypatch.setenv(DASHBOARD_PASSWORD_ENV, PASSWORD)
    monkeypatch.delenv(ENABLE_API_DOCS_ENV, raising=False)


def _client(**kwargs):
    return TestClient(app, **kwargs)


def _assert_challenge(response):
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == CHALLENGE


def _dependency_calls(dependant):
    calls = []
    for sub in dependant.dependencies:
        calls.append(sub.call)
        calls.extend(_dependency_calls(sub))
    return calls


# --- route coverage -------------------------------------------------------------------------------

def test_every_route_except_health_and_webhooks_requires_dashboard_auth():
    for route in app.routes:
        if isinstance(route, APIRoute):
            guarded = require_dashboard_auth in _dependency_calls(route.dependant)
            assert guarded is (route.path not in PUBLIC_ROUTE_PATHS), route.path
        elif isinstance(route, Mount):
            assert route.path == "/static" and isinstance(route.app, AuthenticatedStaticFiles)
        else:
            pytest.fail(f"Unexpected route type {type(route).__name__} at {route.path}")


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_unauthenticated_request_is_challenged_without_opening_a_session(configured, sessions, path):
    response = _client().get(path)

    _assert_challenge(response)
    assert response.json() == {"detail": "Not authenticated"}
    assert sessions == []


def test_unauthenticated_head_request_to_dashboard_is_challenged(configured, sessions):
    _assert_challenge(_client().head("/"))


# --- valid and invalid credentials ----------------------------------------------------------------

def test_correct_credentials_reach_dashboard_static_assets_and_inspection_routes(configured, sessions):
    client = _client()
    auth = (USERNAME, PASSWORD)

    dashboard = client.get("/", auth=auth)
    assert dashboard.status_code == 200 and "Payment Recovery Engine" in dashboard.text
    assert client.head("/", auth=auth).status_code == 200
    assert client.get("/static/js/app.js", auth=auth).status_code == 200
    assert client.get("/static/css/app.css", auth=auth).status_code == 200

    episodes = client.get("/api/v1/episodes", auth=auth)
    assert (episodes.status_code, episodes.json()) == (200, [])
    assert len(sessions) == 1

    rca = client.get("/api/v1/rca/not-a-valid-uuid", auth=auth)
    assert (rca.status_code, rca.json()) == (404, {"detail": "RCA evaluation not found"})


@pytest.mark.parametrize(
    "auth",
    [
        (USERNAME, "wrong-password"),
        ("wrong-user", PASSWORD),
        (USERNAME.upper(), PASSWORD),
        (USERNAME, PASSWORD + " "),
        (USERNAME, ""),
        ("", ""),
    ],
    ids=["wrong-password", "wrong-username", "username-case", "password-suffix", "empty-password", "empty-both"],
)
@pytest.mark.parametrize("path", ["/", "/api/v1/summary", "/static/js/app.js"])
def test_incorrect_credentials_are_rejected(configured, sessions, auth, path):
    response = _client().get(path, auth=auth)

    _assert_challenge(response)
    assert response.json() == {"detail": "Invalid authentication credentials"}
    assert sessions == []


@pytest.mark.parametrize(
    "authorization",
    [
        "Basic !!!not-base64!!!",
        "Basic " + base64.b64encode(b"no-separator").decode(),
        "Bearer " + ENCODED_CREDENTIALS,
        ENCODED_CREDENTIALS,
    ],
    ids=["invalid-base64", "missing-colon", "bearer-scheme", "missing-scheme"],
)
def test_malformed_authorization_headers_are_rejected(configured, sessions, authorization):
    response = _client().get("/api/v1/summary", headers={"Authorization": authorization})

    _assert_challenge(response)
    assert sessions == []


# --- missing configuration fails closed -----------------------------------------------------------

@pytest.mark.parametrize(
    "username, password",
    [(None, None), (USERNAME, None), (None, PASSWORD), ("", ""), ("   ", PASSWORD), (USERNAME, "   ")],
    ids=["both-missing", "password-missing", "username-missing", "both-empty", "blank-username", "blank-password"],
)
@pytest.mark.parametrize("client_auth", [None, (USERNAME, PASSWORD), ("", "")], ids=["anonymous", "creds", "empty"])
@pytest.mark.parametrize("path", ["/", "/api/v1/summary", "/static/js/app.js"])
def test_unconfigured_credentials_fail_closed(
    monkeypatch, sessions, caplog, username, password, client_auth, path
):
    for name, value in ((DASHBOARD_USERNAME_ENV, username), (DASHBOARD_PASSWORD_ENV, password)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    caplog.set_level(logging.DEBUG)

    response = _client().get(path, auth=client_auth)

    assert response.status_code == 503
    assert response.json() == {"detail": "Dashboard temporarily unavailable"}
    assert "www-authenticate" not in response.headers
    assert sessions == []
    assert any(
        r.name == "src.api.security"
        and r.getMessage()
        == f"Dashboard access disabled: {DASHBOARD_USERNAME_ENV} and {DASHBOARD_PASSWORD_ENV} must both be set"
        for r in caplog.records
    )


# --- static files and alternate paths -------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "/static", "/static/", "/static/js/", "/static/index.html", "/static/%2e%2e/main.py",
        "/static/..%2fmain.py", "/index.html", "/api/v1", "/api/v1/", "/api/v1/summary/",
        "/api/v1/timeline/pay_dashboard_test/", "/API/v1/summary",
        # Absolute URL: a bare "//api/..." is a network-path reference that would change the Host header.
        "http://testserver//api/v1/summary",
    ],
)
def test_alternate_paths_never_serve_content_anonymously(configured, sessions, path):
    response = _client().get(path, follow_redirects=True)

    assert response.status_code in (401, 404), response.status_code
    if response.status_code == 401:
        assert response.headers["www-authenticate"] == CHALLENGE
    assert "Payment Recovery Engine" not in response.text
    assert sessions == []


@pytest.mark.parametrize("path", ["/static/%2e%2e/main.py", "/static/..%2fmain.py", "/static/../main.py"])
def test_authenticated_static_requests_cannot_escape_the_static_directory(configured, sessions, path):
    response = _client().get(path, auth=(USERNAME, PASSWORD))

    assert response.status_code == 404
    assert "FastAPI" not in response.text


# --- API documentation ----------------------------------------------------------------------------

def test_default_documentation_routes_are_not_registered_on_the_app():
    assert (app.docs_url, app.redoc_url, app.openapi_url) == (None, None, None)


@pytest.mark.parametrize("flag", [None, "", "false", "0", "no", "off", "enabled"])
@pytest.mark.parametrize("path", DOCS_PATHS + ["/docs/oauth2-redirect"])
@pytest.mark.parametrize("auth", [None, (USERNAME, PASSWORD)], ids=["anonymous", "authenticated"])
def test_documentation_is_disabled_unless_explicitly_enabled(configured, monkeypatch, flag, path, auth):
    if flag is None:
        monkeypatch.delenv(ENABLE_API_DOCS_ENV, raising=False)
    else:
        monkeypatch.setenv(ENABLE_API_DOCS_ENV, flag)

    response = _client().get(path, auth=auth)

    assert (response.status_code, response.json()) == (404, {"detail": "Not Found"})
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize("flag", ["true", "TRUE", "1", "yes"])
@pytest.mark.parametrize("path", DOCS_PATHS)
def test_enabled_documentation_still_requires_dashboard_credentials(configured, monkeypatch, flag, path):
    monkeypatch.setenv(ENABLE_API_DOCS_ENV, flag)

    _assert_challenge(_client().get(path))
    _assert_challenge(_client().get(path, auth=(USERNAME, "wrong-password")))


def test_enabled_documentation_is_served_to_authenticated_users(configured, monkeypatch):
    monkeypatch.setenv(ENABLE_API_DOCS_ENV, "true")
    client = _client()
    auth = (USERNAME, PASSWORD)

    schema = client.get("/openapi.json", auth=auth)
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert "/api/v1/summary" in paths
    assert not {"/docs", "/redoc", "/openapi.json", "/"} & set(paths)

    swagger = client.get("/docs", auth=auth)
    assert swagger.status_code == 200 and "swagger-ui" in swagger.text and "/openapi.json" in swagger.text
    redoc = client.get("/redoc", auth=auth)
    assert redoc.status_code == 200 and "redoc" in redoc.text.lower()


def test_enabled_documentation_fails_closed_without_configured_credentials(monkeypatch):
    monkeypatch.setenv(ENABLE_API_DOCS_ENV, "true")
    monkeypatch.delenv(DASHBOARD_USERNAME_ENV, raising=False)
    monkeypatch.delenv(DASHBOARD_PASSWORD_ENV, raising=False)

    for path in DOCS_PATHS:
        response = _client().get(path, auth=(USERNAME, PASSWORD))
        assert (response.status_code, response.json()) == (503, {"detail": "Dashboard temporarily unavailable"})


# --- trusted Host headers -------------------------------------------------------------------------

@pytest.mark.parametrize("base_url", ["http://localhost", "http://localhost:8000", "http://127.0.0.1:8000"])
def test_default_allowed_hosts_accept_local_hosts(monkeypatch, sessions, base_url):
    monkeypatch.delenv(ALLOWED_HOSTS_ENV, raising=False)

    response = _client(base_url=base_url).get("/health")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})


@pytest.mark.parametrize(
    "base_url", ["http://evil.example", "http://testserver", "http://192.168.1.20:8000", "http://localhost.evil.example"]
)
@pytest.mark.parametrize("path", ["/health", "/", "/api/v1/summary", "/static/js/app.js"])
def test_default_allowed_hosts_reject_other_hosts_before_routing(monkeypatch, configured, sessions, base_url, path):
    monkeypatch.delenv(ALLOWED_HOSTS_ENV, raising=False)

    response = _client(base_url=base_url).get(path, auth=(USERNAME, PASSWORD))

    assert (response.status_code, response.text) == (400, "Invalid host header")
    assert sessions == []


def test_configured_allowed_hosts_replace_the_defaults(monkeypatch, sessions):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "demo.example.test, *.tunnel.example")

    assert _client(base_url="http://demo.example.test").get("/health").status_code == 200
    assert _client(base_url="https://abc.tunnel.example").get("/health").status_code == 200
    assert _client(base_url="http://localhost").get("/health").status_code == 400
    assert _client(base_url="http://tunnel.example.evil.test").get("/health").status_code == 400


def test_disallowed_host_rejects_webhooks_without_invoking_ingestion(monkeypatch, sessions):
    monkeypatch.delenv(ALLOWED_HOSTS_ENV, raising=False)
    monkeypatch.setenv(RAZORPAY_WEBHOOK_SECRET_ENV, "host-check-test-secret")

    response = _client(base_url="http://abc.ngrok-free.app").post(
        "/ingest/razorpay", content=b"{}", headers={"X-Razorpay-Signature": "sig", "X-Razorpay-Event-Id": "evt"}
    )

    assert (response.status_code, response.text) == (400, "Invalid host header")
    assert sessions == []


WILDCARD_ALL = "must not allow every host"
MALFORMED = "contains a malformed entry"
BROAD_WILDCARD = "contains an overly broad wildcard entry"
SHARED_SUFFIX = "contains a shared-suffix wildcard entry"
REQUIRED_DENIED_SUFFIXES = ["co.uk", "org.uk", "com.au", "co.in", "ngrok-free.app"]
INVALID_ALLOWED_HOSTS = [
    ("*", WILDCARD_ALL),
    (" * ", WILDCARD_ALL),
    ("marker-host.example,*", WILDCARD_ALL),
    ("*,127.0.0.1,marker-host.example", WILDCARD_ALL),
    ("marker-host*", MALFORMED),
    ("*marker-host.example", MALFORMED),
    ("marker-host.*.example", MALFORMED),
    ("**.marker-host.example", MALFORMED),
    ("*.", MALFORMED),
    ("localhost,marker-host.example:8000", MALFORMED),
    ("http://marker-host.example", MALFORMED),
    ("marker host.example", MALFORMED),
    ("marker_host.example", MALFORMED),
    ("-marker-host.example", MALFORMED),
    ("marker-host-.example", MALFORMED),
    ("marker-host..example", MALFORMED),
    ("marker-host.example/path", MALFORMED),
    ("*.com", BROAD_WILDCARD),
    ("*.io", BROAD_WILDCARD),
    ("*.localhost", BROAD_WILDCARD),
    ("*.marker-host", BROAD_WILDCARD),
    ("localhost,127.0.0.1,*.marker-host", BROAD_WILDCARD),
    ("*.example.com,*.marker-host", BROAD_WILDCARD),
    ("*.0.1", BROAD_WILDCARD),
    ("*.marker-host.123", BROAD_WILDCARD),
    *[(f"*.{suffix}", SHARED_SUFFIX) for suffix in REQUIRED_DENIED_SUFFIXES],
    ("*.NGROK-FREE.APP", SHARED_SUFFIX),
    ("localhost,127.0.0.1,marker-host.example,*.co.uk", SHARED_SUFFIX),
    ("*.example.com,*.ngrok-free.app,demo-1.ngrok-free.app", SHARED_SUFFIX),
    ("*.marker-host.example,*.com.au", SHARED_SUFFIX),
    # The earlier checks report first when a list has several kinds of problem.
    ("*.co.uk,marker-host*", MALFORMED),
    ("*.co.in,*.com", BROAD_WILDCARD),
]


def test_required_shared_suffixes_are_denylisted():
    assert set(REQUIRED_DENIED_SUFFIXES) <= SHARED_SUFFIX_WILDCARD_DENYLIST


@pytest.mark.parametrize("suffix", sorted(SHARED_SUFFIX_WILDCARD_DENYLIST))
def test_every_denylisted_suffix_is_rejected_as_a_wildcard(monkeypatch, suffix):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, f"localhost,*.{suffix}")

    with pytest.raises(InvalidAllowedHostsError) as excinfo:
        get_allowed_hosts()

    assert str(excinfo.value) == f"{ALLOWED_HOSTS_ENV} {SHARED_SUFFIX}"
    assert suffix not in str(excinfo.value)


@pytest.mark.parametrize("configured_hosts, reason", INVALID_ALLOWED_HOSTS)
def test_unusable_allowed_hosts_are_rejected_by_validation(monkeypatch, configured_hosts, reason):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, configured_hosts)

    with pytest.raises(InvalidAllowedHostsError) as excinfo:
        get_allowed_hosts()

    assert str(excinfo.value) == f"{ALLOWED_HOSTS_ENV} {reason}"


@pytest.mark.parametrize("configured_hosts, reason", INVALID_ALLOWED_HOSTS)
@pytest.mark.parametrize(
    "method, path, base_url",
    [
        ("GET", "/health", "http://localhost"),
        ("GET", "/", "http://localhost"),
        ("GET", "/api/v1/summary", "http://marker-host.example"),
        ("GET", "/static/js/app.js", "http://evil.example"),
        ("POST", "/ingest/razorpay", "http://localhost"),
    ],
)
def test_unusable_allowed_hosts_fail_closed_without_500_or_leaking_configuration(
    monkeypatch, configured, sessions, caplog, configured_hosts, reason, method, path, base_url
):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, configured_hosts)
    caplog.set_level(logging.DEBUG)

    response = _client(base_url=base_url).request(method, path, auth=(USERNAME, PASSWORD))

    assert response.status_code == 503
    assert response.json() == {"detail": "Service temporarily unavailable"}
    assert sessions == []
    assert any(
        r.name == "src.api.security" and r.getMessage() == f"Host validation failed closed: {ALLOWED_HOSTS_ENV} {reason}"
        for r in caplog.records
    )
    assert "marker" not in response.text and "*" not in response.text
    # httpx logs the request URL, which is the test's base_url rather than configuration.
    app_logs = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    assert "marker" not in app_logs and "*" not in app_logs
    for entry in filter(None, (e.strip() for e in configured_hosts.split(","))):
        assert entry not in response.text and entry not in app_logs


def test_unusable_allowed_hosts_close_websocket_connections(monkeypatch):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "*")

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with _client().websocket_connect("/ws"):
            pass

    assert excinfo.value.code == 1011


def test_unusable_allowed_hosts_do_not_block_application_startup(monkeypatch, sessions):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "marker-host*")
    monkeypatch.setattr(app.state, "paypal_client", None, raising=False)

    with TestClient(app) as client:
        response = client.get("/health")

    assert (response.status_code, response.json()) == (503, {"detail": "Service temporarily unavailable"})


@pytest.mark.parametrize(
    "configured_hosts, expected",
    [
        (None, list(DEFAULT_ALLOWED_HOSTS)),
        ("", list(DEFAULT_ALLOWED_HOSTS)),
        (" , ", list(DEFAULT_ALLOWED_HOSTS)),
        ("localhost, 127.0.0.1 ,", ["localhost", "127.0.0.1"]),
        ("*.example.com,api.example.com", ["*.example.com", "api.example.com"]),
        ("localhost,127.0.0.1,*.example.com", ["localhost", "127.0.0.1", "*.example.com"]),
        ("*.tunnel.example", ["*.tunnel.example"]),
        ("*.eu.example.com", ["*.eu.example.com"]),
        ("*.example2.com", ["*.example2.com"]),
        ("demo-1.ngrok-free.app", ["demo-1.ngrok-free.app"]),
        (
            "localhost,127.0.0.1,*.example.com,*.tunnel.example,*.eu.example.com,*.example2.com",
            ["localhost", "127.0.0.1", "*.example.com", "*.tunnel.example", "*.eu.example.com", "*.example2.com"],
        ),
        # A specific domain under a shared suffix is one party's domain, not the shared suffix itself.
        ("*.mybrand.co.uk", ["*.mybrand.co.uk"]),
        ("*.demo-1.ngrok-free.app", ["*.demo-1.ngrok-free.app"]),
        ("shop.example.co.in,co.uk", ["shop.example.co.in", "co.uk"]),
    ],
)
def test_valid_allowed_hosts_are_accepted(monkeypatch, configured_hosts, expected):
    if configured_hosts is None:
        monkeypatch.delenv(ALLOWED_HOSTS_ENV, raising=False)
    else:
        monkeypatch.setenv(ALLOWED_HOSTS_ENV, configured_hosts)

    assert get_allowed_hosts() == expected


@pytest.mark.parametrize(
    "base_url, expected_status",
    [
        ("http://localhost:8000", 200),
        ("http://127.0.0.1", 200),
        ("https://a.example.com", 200),
        ("https://deep.a.example.com", 200),
        ("https://example.com", 400),
        ("https://a.example.com.evil.test", 400),
    ],
)
def test_supported_subdomain_patterns_still_route(monkeypatch, sessions, base_url, expected_status):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "localhost,127.0.0.1,*.example.com")

    assert _client(base_url=base_url).get("/health").status_code == expected_status


@pytest.mark.parametrize(
    "base_url, expected_status",
    [
        ("https://shop.mybrand.co.uk", 200),
        ("https://demo-1.ngrok-free.app", 200),
        ("https://other.co.uk", 400),
        ("https://someone-else.ngrok-free.app", 400),
    ],
)
def test_specific_domains_under_shared_suffixes_route_only_to_their_own_hosts(
    monkeypatch, sessions, base_url, expected_status
):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "*.mybrand.co.uk,demo-1.ngrok-free.app")

    assert _client(base_url=base_url).get("/health").status_code == expected_status


# --- public routes keep their behavior ------------------------------------------------------------

def test_health_stays_public_when_dashboard_credentials_are_not_configured(monkeypatch, sessions):
    monkeypatch.delenv(DASHBOARD_USERNAME_ENV, raising=False)
    monkeypatch.delenv(DASHBOARD_PASSWORD_ENV, raising=False)

    response = _client().get("/health")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})
    assert "www-authenticate" not in response.headers


def test_webhooks_do_not_use_dashboard_auth(monkeypatch, sessions):
    monkeypatch.delenv(DASHBOARD_USERNAME_ENV, raising=False)
    monkeypatch.delenv(DASHBOARD_PASSWORD_ENV, raising=False)
    monkeypatch.delenv(RAZORPAY_WEBHOOK_SECRET_ENV, raising=False)
    app.dependency_overrides.pop(get_ingestion_service, None)
    monkeypatch.setattr(app.state, "paypal_client", None, raising=False)
    client = _client()

    razorpay = client.post(
        "/ingest/razorpay", content=b"{}", headers={"X-Razorpay-Signature": "sig", "X-Razorpay-Event-Id": "evt"}
    )
    paypal = client.post("/webhooks/paypal", content=b"{}")

    assert (razorpay.status_code, razorpay.json()) == (503, {"detail": "Webhook intake temporarily unavailable"})
    assert (paypal.status_code, paypal.json()) == (503, {"detail": "PayPal webhook intake is not configured"})
    for response in (razorpay, paypal):
        assert "www-authenticate" not in response.headers
    assert sessions == []


# --- no credential leakage ------------------------------------------------------------------------

def test_credentials_never_appear_in_responses_or_logs(configured, monkeypatch, sessions, caplog):
    monkeypatch.setenv(ENABLE_API_DOCS_ENV, "true")
    caplog.set_level(logging.DEBUG)
    client = _client()
    responses = [
        client.get("/api/v1/episodes", auth=(USERNAME, PASSWORD)),
        client.get("/openapi.json", auth=(USERNAME, PASSWORD)),
        client.get("/api/v1/summary", auth=(USERNAME, "wrong-password")),
        client.get("/api/v1/summary", auth=("wrong-user", PASSWORD)),
        client.get("/static/js/app.js", headers={"Authorization": "Basic " + ENCODED_CREDENTIALS + "x"}),
        client.get("/", auth=(USERNAME, PASSWORD)),
    ]
    monkeypatch.delenv(DASHBOARD_PASSWORD_ENV)
    responses.append(client.get("/api/v1/summary", auth=(USERNAME, PASSWORD)))

    secrets_and_tokens = (PASSWORD, ENCODED_CREDENTIALS, USERNAME)
    for response in responses:
        for value in secrets_and_tokens:
            assert value not in response.text
            assert value not in str(response.headers)
    for value in secrets_and_tokens:
        assert value not in caplog.text

"""What the application needs to stand on a public address.

Sign-in itself lives in admin_auth. This is everything around it: which host
names the site answers to, sending browsers to HTTPS, the headers that keep a
page from being framed or sniffed, reading the visitor's real address from
behind a reverse proxy, and refusing to start a production deployment that is
missing its secrets.

Settings, all optional in development:

    PHARMASEARCH_ENV               "production" turns the startup checks on
    PHARMASEARCH_ALLOWED_HOSTS     host names this site answers to, comma
                                   separated ("search.example.com")
    PHARMASEARCH_FORCE_HTTPS       "1" to redirect http to https and mark the
                                   session cookie Secure
    PHARMASEARCH_TRUSTED_PROXY     "1" when a reverse proxy in front sets
                                   X-Forwarded-Proto and X-Forwarded-For
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

import config  # noqa: F401 -- loads Backend/.env before the variables are read
from core.logging_config import get_logger


logger = get_logger(__name__)

# A page of this application is never framed, loads only its own assets, and
# sends no referrer to another site. The styles and scripts it does load are
# its own files plus the CDN the templates use.
CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' https://cdn.jsdelivr.net data:; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
}
HSTS = "max-age=31536000; includeSubDomains"


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def is_production() -> bool:
    return (os.getenv("PHARMASEARCH_ENV") or "").strip().lower() in {"production", "prod", "live"}


def force_https() -> bool:
    return _flag("PHARMASEARCH_FORCE_HTTPS")


def trusts_proxy() -> bool:
    return _flag("PHARMASEARCH_TRUSTED_PROXY")


def allowed_hosts() -> list[str]:
    """The host names this site answers to.

    Render names a service only once it exists, so its blueprint cannot write
    the name down. It does hand the name to the running service, and that
    comes from the platform, not the visitor, so it is safe to trust.
    """
    configured = os.getenv("PHARMASEARCH_ALLOWED_HOSTS") or os.getenv("RENDER_EXTERNAL_HOSTNAME") or ""
    names = [name.strip() for name in configured.split(",")]
    return [name for name in names if name]


def request_is_secure(request: Request) -> bool:
    """True when the browser reached the site over HTTPS.

    A reverse proxy terminates TLS and speaks plain HTTP to the application, so
    the scheme it reports is http. Its X-Forwarded-Proto header carries what
    the browser actually used, and is believed only when a proxy is declared.
    """
    if force_https():
        return True
    if trusts_proxy():
        forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if forwarded:
            return forwarded == "https"
    return request.url.scheme == "https"


def client_address(request: Request) -> str:
    """The visitor's address, read through a declared proxy where there is one.

    X-Forwarded-For is written by whoever sends the request, so it is read only
    when a trusted proxy is declared; the last entry is the one that proxy saw.
    """
    if trusts_proxy():
        forwarded = request.headers.get("x-forwarded-for") or ""
        addresses = [part.strip() for part in forwarded.split(",") if part.strip()]
        if addresses:
            return addresses[-1]
    return request.client.host if request.client else "unknown"


def configuration_problems() -> list[str]:
    """What would leave a public deployment unsafe, in plain words."""
    from services.admin_auth import admin_is_configured

    problems = []
    if not admin_is_configured():
        problems.append(
            "No admin sign-in is configured: set PHARMASEARCH_ADMIN_USERNAME and either "
            "PHARMASEARCH_ADMIN_PASSWORD (at least 12 characters) or "
            "PHARMASEARCH_ADMIN_PASSWORD_HASH (tools/make_admin_password.py prints the hash)."
        )
    if not (os.getenv("PHARMASEARCH_SESSION_SECRET") or "").strip():
        problems.append(
            "PHARMASEARCH_SESSION_SECRET is not set: sessions would be signed with a key that "
            "changes on every restart, signing everyone out."
        )
    if not allowed_hosts():
        problems.append(
            "PHARMASEARCH_ALLOWED_HOSTS is not set: the site would answer to any host name."
        )
    if not (force_https() or trusts_proxy()):
        problems.append(
            "Neither PHARMASEARCH_FORCE_HTTPS nor PHARMASEARCH_TRUSTED_PROXY is set: the session "
            "cookie would not be marked Secure."
        )
    return problems


def check_production_configuration() -> None:
    """Stop a production start that is missing its secrets, saying which."""
    if not is_production():
        return
    problems = configuration_problems()
    if problems:
        raise RuntimeError(
            "PHARMASEARCH_ENV=production, but the deployment is not configured:\n- "
            + "\n- ".join(problems)
        )


class _HostCheck(TrustedHostMiddleware):
    """The host allow-list, except for the health check.

    A platform probes /healthz on the container's own address, under a host
    name that is not the site's. Refusing it would fail every deploy; the
    route says only that the process is up.
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/healthz":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def add_security(app: FastAPI) -> None:
    """Host checking, HTTPS redirection and the response headers."""
    hosts = allowed_hosts()
    if hosts:
        app.add_middleware(_HostCheck, allowed_hosts=hosts)

    @app.middleware("http")
    async def secure_headers(request: Request, call_next):
        if force_https() and not request_is_secure(request):
            target = request.url.replace(scheme="https")
            if request.method in ("GET", "HEAD"):
                return RedirectResponse(str(target), status_code=308)
            return JSONResponse({"detail": "HTTPS is required"}, status_code=400)
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if request_is_secure(request):
            response.headers.setdefault("Strict-Transport-Security", HSTS)
        return response

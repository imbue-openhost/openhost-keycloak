#!/usr/bin/env python3
"""Front proxy for Keycloak on OpenHost.

Listens on the OpenHost-routed port and forwards to Keycloak on loopback.
Responsibilities:

1. Health endpoint: serves ``/_healthz`` with a static 200 so OpenHost's
   readiness probe succeeds immediately, and serves a 200 placeholder on
   ``/`` while Keycloak is still starting (OpenHost only waits 60s and
   treats any status < 500 as ready).

2. Owner gating (defense in depth): OpenHost's ``public_paths`` for this
   app includes ``/realms/`` so anonymous customers can reach OIDC
   endpoints and realm login pages. The Keycloak *admin* surface must not
   be anonymous, so requests to ``/admin`` and ``/realms/master`` are
   rejected unless the OpenHost router stamped ``X-OpenHost-Is-Owner:
   true`` (the router strips any client-supplied ``X-OpenHost-*`` headers,
   so the header is trustworthy).

3. Owner auto-login (OpenHost SSO): when the OpenHost owner navigates to
   an owner-only page (e.g. the admin console) without a Keycloak session,
   the proxy drives Keycloak's own browser login on loopback using the
   per-boot bootstrap admin credentials (``SSO_USER`` / ``SSO_PASSWORD``,
   passed via environment by start.sh, never written to disk) and replays
   Keycloak's session cookies onto the visitor's browser. The owner lands
   in the admin console without ever seeing a Keycloak login form.

4. Header hygiene: rewrites ``Host`` from ``X-Forwarded-Host`` (Keycloak
   builds issuer/redirect URLs from forwarded headers), guarantees
   ``X-Forwarded-Proto`` is set, and strips ``X-OpenHost-*`` headers
   before forwarding upstream.

5. Basic abuse hardening: request body size cap and client socket
   timeouts (this port serves anonymous internet traffic).

Stdlib only; no third-party dependencies.
"""

import base64
import hashlib
import html
import http.client
import http.server
import json
import os
import posixpath
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse

LISTEN_PORT = int(os.environ.get("PROXY_LISTEN_PORT", "8080"))
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = int(os.environ.get("KC_UPSTREAM_PORT", "8081"))
MANAGEMENT_PORT = int(os.environ.get("KC_MANAGEMENT_PORT", "9000"))

# Owner requests (admin console, realm imports) get a generous cap;
# anonymous internet traffic (login forms, token requests) needs far less,
# and a small cap limits memory-exhaustion abuse on this public port.
MAX_BODY_BYTES_OWNER = 64 * 1024 * 1024
MAX_BODY_BYTES_ANON = 1 * 1024 * 1024

# The public scheme of the zone. TLS terminates at the zone's Caddy, and
# the OpenHost router forwards X-Forwarded-Proto as "http" (its own inbound
# scheme), which would make Keycloak mint http:// issuer/redirect URLs.
# Force the real external scheme; dev zones (lvh.me/localhost) are http.
_ZONE_DOMAIN = os.environ.get("OPENHOST_ZONE_DOMAIN", "")
EXTERNAL_SCHEME = os.environ.get(
    "EXTERNAL_SCHEME",
    "http"
    if ("lvh.me" in _ZONE_DOMAIN or "localhost" in _ZONE_DOMAIN or not _ZONE_DOMAIN)
    else "https",
)
CLIENT_TIMEOUT_SECONDS = 60
UPSTREAM_TIMEOUT_SECONDS = 120

OWNER_HEADER = "X-OpenHost-Is-Owner"

_APP_NAME = os.environ.get("OPENHOST_APP_NAME", "keycloak")
DEFAULT_PUBLIC_HOST = f"{_APP_NAME}.{_ZONE_DOMAIN}" if _ZONE_DOMAIN else "localhost"

# Per-boot bootstrap admin used for owner auto-login. start.sh generates a
# fresh random pair on every container start and passes it via environment;
# neither value ever touches disk. Empty values disable auto-login (the
# owner then sees Keycloak's own login form).
SSO_USER = os.environ.get("SSO_USER", "")
SSO_PASSWORD = os.environ.get("SSO_PASSWORD", "")
SSO_USER_PREFIX = "openhost-sso-"

# Keycloak's master-realm session cookie. It is scoped Path=/realms/master/,
# so the browser never sends it on /admin or / navigations -- the proxy
# cannot see it there to know the owner is already logged in. We therefore
# set our own Path=/ marker cookie alongside the Keycloak cookies; it
# expires before Keycloak's default 30-minute SSO idle timeout, so an
# expired Keycloak session simply triggers a fresh (silent) auto-login.
SESSION_COOKIE = "KEYCLOAK_IDENTITY"
MARKER_COOKIE = "OPENHOST_KC_SSO"
MARKER_MAX_AGE = 25 * 60

# Paths anonymous visitors may reach. MUST mirror routing.public_paths in
# openhost.toml. The owner is never auto-logged-in on these paths: customer
# realm logins (and the master-realm OIDC endpoints the admin console uses
# *after* the SSO cookie exists) must flow through untouched.
PUBLIC_PATH_PREFIXES = ("/realms/", "/resources/", "/js/")
PUBLIC_PATH_EXACT = ("/robots.txt",)

# Paths (as decoded, normalized segment tuples) that require the OpenHost
# owner header. Keep in sync with the rationale in openhost.toml.
OWNER_ONLY_SEGMENT_PREFIXES = (
    ("admin",),
    ("realms", "master"),
)

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

QUIET_PATHS = {"/_healthz", "/"}

PLACEHOLDER_HTML = b"""<!doctype html>
<html><head><title>Keycloak is starting</title>
<meta http-equiv="refresh" content="5"></head>
<body><h1>Keycloak is starting&hellip;</h1>
<p>This page refreshes automatically.</p></body></html>
"""


def normalized_segments(raw_path: str) -> list:
    """Decode and normalize a request path into clean segments.

    Defends the owner-only check against percent-encoding
    (``/realms/%6daster``), dot segments (``/realms/x/../master``),
    duplicate slashes, backslashes, and path-parameter (``;``) tricks.
    """
    path = raw_path.split("?", 1)[0].split("#", 1)[0]
    # Decode twice to also catch double-encoded sequences (%256d).
    path = urllib.parse.unquote(urllib.parse.unquote(path))
    path = path.replace("\\", "/")
    path = posixpath.normpath(path)
    segments = []
    for segment in path.split("/"):
        segment = segment.split(";", 1)[0]
        if segment in ("", "."):
            continue
        segments.append(segment.lower())
    return segments


def is_owner_only(raw_path: str) -> bool:
    segments = normalized_segments(raw_path)
    for prefix in OWNER_ONLY_SEGMENT_PREFIXES:
        if tuple(segments[: len(prefix)]) == prefix:
            return True
    return False


def is_public_path(raw_path: str) -> bool:
    path = raw_path.split("?", 1)[0]
    return path in PUBLIC_PATH_EXACT or path.startswith(PUBLIC_PATH_PREFIXES)


# --------------------------------------------------------- owner auto-login


class LoginError(Exception):
    pass


def _log(message: str):
    sys.stderr.write("[proxy] %s\n" % message)
    sys.stderr.flush()


def _sso_request(method: str, path: str, headers: dict, body: bytes = None, port: int = UPSTREAM_PORT):
    """One loopback HTTP request; returns (status, headers, body)."""
    connection = http.client.HTTPConnection(UPSTREAM_HOST, port, timeout=30)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        return response.status, response.headers, data
    finally:
        connection.close()


def _sso_base_headers(public_host: str) -> dict:
    return {
        "Host": public_host,
        "X-Forwarded-Host": public_host,
        "X-Forwarded-Proto": EXTERNAL_SCHEME,
        "X-Forwarded-For": "127.0.0.1",
        "Accept": "text/html,application/xhtml+xml",
        "User-Agent": "openhost-keycloak-proxy",
        "Connection": "close",
    }


def _cookie_header_from(set_cookies: list) -> str:
    pairs = []
    for set_cookie in set_cookies:
        first = set_cookie.split(";", 1)[0].strip()
        if "=" in first:
            pairs.append(first)
    return "; ".join(pairs)


def perform_owner_login(public_host: str) -> list:
    """Drive Keycloak's own browser login as the per-boot bootstrap admin.

    Initiates a synthetic admin-console OIDC flow on loopback, submits the
    login form, and returns the resulting ``Set-Cookie`` values so they can
    be replayed onto the owner's browser. The authorization code produced
    by the flow is deliberately abandoned: only the master-realm SSO
    session cookie matters -- with it set, the real admin-console OIDC
    redirect completes silently. Raises LoginError when the flow fails.
    """
    if not SSO_USER or not SSO_PASSWORD:
        raise LoginError("SSO credentials not configured")

    code_verifier = secrets.token_urlsafe(48)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    redirect_uri = f"{EXTERNAL_SCHEME}://{public_host}/admin/master/console/"
    auth_path = "/realms/master/protocol/openid-connect/auth?" + urllib.parse.urlencode(
        {
            "client_id": "security-admin-console",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid",
            "state": secrets.token_urlsafe(16),
            "nonce": secrets.token_urlsafe(16),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )

    status, headers, body = _sso_request("GET", auth_path, _sso_base_headers(public_host))
    if status != 200:
        raise LoginError(f"auth endpoint returned {status}")
    auth_cookies = headers.get_all("Set-Cookie") or []

    match = re.search(r'<form[^>]+action="([^"]+)"', body.decode("utf-8", "replace"))
    if not match:
        raise LoginError("could not find login form action")
    action = urllib.parse.urlparse(html.unescape(match.group(1)))
    form_path = action.path + (f"?{action.query}" if action.query else "")

    form_headers = _sso_base_headers(public_host)
    form_headers["Content-Type"] = "application/x-www-form-urlencoded"
    form_headers["Cookie"] = _cookie_header_from(auth_cookies)
    form_body = urllib.parse.urlencode(
        {"username": SSO_USER, "password": SSO_PASSWORD, "credentialId": ""}
    ).encode()
    status, headers, _ = _sso_request("POST", form_path, form_headers, form_body)
    location = headers.get("Location") or ""
    if status not in (302, 303) or "code=" not in location:
        raise LoginError(f"login POST returned {status} (location={location!r})")
    login_cookies = headers.get_all("Set-Cookie") or []

    # Merge by cookie name (login-response values win) and replay only the
    # KEYCLOAK_* session cookies. Replaying the consumed AUTH_SESSION_ID
    # cookie would break the browser's subsequent silent SSO (Keycloak then
    # shows the login form instead of issuing a code).
    merged = {}
    for set_cookie in auth_cookies + login_cookies:
        name = set_cookie.split("=", 1)[0].strip()
        if name.startswith("KEYCLOAK_"):
            merged[name] = set_cookie
    if SESSION_COOKIE not in merged:
        raise LoginError("login flow did not produce an identity cookie")
    return list(merged.values())


def _get_admin_token(public_host: str):
    headers = _sso_base_headers(public_host)
    headers["Accept"] = "application/json"
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    body = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": SSO_USER,
            "password": SSO_PASSWORD,
        }
    ).encode()
    status, _, data = _sso_request(
        "POST", "/realms/master/protocol/openid-connect/token", headers, body
    )
    if status != 200:
        _log(f"admin token request failed: {status}")
        return None
    return json.loads(data)["access_token"]


def _wait_for_keycloak_ready(timeout: float = 600.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _, _ = _sso_request(
                "GET", "/health/ready", {"Connection": "close"}, port=MANAGEMENT_PORT
            )
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(3)
    return False


def cleanup_stale_sso_users():
    """Delete per-boot bootstrap admins left behind by previous boots."""
    if not SSO_USER or not SSO_PASSWORD:
        return
    public_host = DEFAULT_PUBLIC_HOST
    try:
        if not _wait_for_keycloak_ready():
            _log("keycloak never became ready; skipping stale SSO-user cleanup")
            return
        token = _get_admin_token(public_host)
        if token is None:
            _log("could not obtain admin token; skipping stale SSO-user cleanup")
            return
        api_headers = _sso_base_headers(public_host)
        api_headers["Accept"] = "application/json"
        api_headers["Authorization"] = f"Bearer {token}"
        query = urllib.parse.urlencode({"username": SSO_USER_PREFIX, "max": "200"})
        status, _, data = _sso_request(
            "GET", f"/admin/realms/master/users?{query}", api_headers
        )
        if status != 200:
            _log(f"stale SSO-user search failed: {status}")
            return
        removed = 0
        for user in json.loads(data):
            username = user.get("username", "")
            if username.startswith(SSO_USER_PREFIX) and username != SSO_USER:
                status, _, _ = _sso_request(
                    "DELETE", f"/admin/realms/master/users/{user['id']}", api_headers
                )
                if status == 204:
                    removed += 1
                else:
                    _log(f"failed to delete stale SSO user {username}: {status}")
        _log(f"stale SSO-user cleanup done ({removed} removed)")
    except Exception as exc:  # never kill the proxy over cleanup
        _log(f"stale SSO-user cleanup error: {exc}")


class KeycloakProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "openhost-keycloak-proxy"
    sys_version = ""
    timeout = CLIENT_TIMEOUT_SECONDS

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt, *args):  # noqa: N802
        if self.path in QUIET_PATHS:
            return
        sys.stderr.write("[proxy] %s - %s\n" % (self.address_string(), fmt % args))

    def _send_simple(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8", extra_headers=None):
        # Locally-generated responses (health, placeholder, rejections) may
        # be sent without the request body having been read. Close the
        # connection so unread body bytes can never be parsed as a
        # follow-up request on a kept-alive connection (request desync).
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ------------------------------------------------------------- methods
    def do_GET(self):  # noqa: N802
        self._handle()

    def do_HEAD(self):  # noqa: N802
        self._handle()

    def do_POST(self):  # noqa: N802
        self._handle()

    def do_PUT(self):  # noqa: N802
        self._handle()

    def do_DELETE(self):  # noqa: N802
        self._handle()

    def do_PATCH(self):  # noqa: N802
        self._handle()

    def do_OPTIONS(self):  # noqa: N802
        self._handle()

    # -------------------------------------------------------------- core
    def _handle(self):
        bare_path = self.path.split("?", 1)[0]
        if bare_path == "/_healthz":
            self._send_simple(200, b"ok\n")
            return
        if bare_path == "/robots.txt":
            # A public IdP's login pages have no business being indexed
            # (Keycloak itself 404s robots.txt).
            self._send_simple(200, b"User-agent: *\nDisallow: /\n")
            return

        is_owner = (self.headers.get(OWNER_HEADER) or "").strip().lower() == "true"
        if is_owner_only(self.path) and not is_owner:
            # 404 (not 403) so the admin surface is indistinguishable from
            # a missing route for anonymous scanners.
            self._send_simple(404, b"not found\n")
            return

        if is_owner and self._should_auto_login() and self._try_auto_login():
            return

        body, error = self._read_body(
            MAX_BODY_BYTES_OWNER if is_owner else MAX_BODY_BYTES_ANON
        )
        if error:
            return

        upstream_headers = self._build_upstream_headers(body)
        self._forward(body, upstream_headers)

    # ----------------------------------------------------- owner auto-login
    def _should_auto_login(self) -> bool:
        """Owner HTML navigation to a non-public page without a session."""
        if self.command != "GET":
            return False
        if is_public_path(self.path):
            return False
        if "text/html" not in (self.headers.get("Accept") or "").lower():
            return False
        cookies = self.headers.get("Cookie") or ""
        if f"{MARKER_COOKIE}=" in cookies:
            return False
        return f"{SESSION_COOKIE}=" not in cookies

    def _try_auto_login(self) -> bool:
        """Mint a Keycloak session for the owner; True when handled."""
        public_host = (
            self.headers.get("X-Forwarded-Host")
            or self.headers.get("Host")
            or DEFAULT_PUBLIC_HOST
        )
        try:
            session_cookies = perform_owner_login(public_host)
        except LoginError as exc:
            _log(f"owner auto-login failed, falling through to proxy: {exc}")
            return False
        except (http.client.HTTPException, socket.timeout, OSError):
            # Keycloak unreachable (cold start); let the normal forwarding
            # path produce the placeholder / 503.
            return False
        marker = f"{MARKER_COOKIE}=1; Path=/; Max-Age={MARKER_MAX_AGE}; HttpOnly; SameSite=Lax"
        if EXTERNAL_SCHEME == "https":
            marker += "; Secure"
        self.close_connection = True
        try:
            self.send_response(302)
            self.send_header("Location", self.path)
            for cookie in session_cookies:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Set-Cookie", marker)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass
        _log("owner auto-login performed")
        return True

    def _read_body(self, max_body_bytes: int):
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            self._send_simple(411, b"chunked transfer encoding not supported; send Content-Length\n")
            return None, True
        length_header = self.headers.get("Content-Length")
        if not length_header:
            return b"", False
        try:
            length = int(length_header)
        except ValueError:
            self._send_simple(400, b"bad Content-Length\n")
            return None, True
        if length < 0:
            self._send_simple(400, b"bad Content-Length\n")
            return None, True
        if length > max_body_bytes:
            self._send_simple(413, b"request body too large\n")
            return None, True
        try:
            body = self.rfile.read(length)
        except socket.timeout:
            self._send_simple(408, b"timed out reading request body\n")
            return None, True
        if len(body) != length:
            # Short read; client went away or lied about length. No
            # response can safely be written; just drop the connection.
            self.close_connection = True
            return None, True
        return body, False

    def _build_upstream_headers(self, body: bytes) -> dict:
        headers = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS:
                continue
            if lower.startswith("x-openhost-"):
                continue
            if lower in ("host", "content-length"):
                continue
            # Collapse duplicate header lines: cookies join with "; "
            # (RFC 6265), everything else with ", " (RFC 9110).
            separator = "; " if lower == "cookie" else ", "
            merged = False
            for existing in list(headers):
                if existing.lower() == lower:
                    headers[existing] = headers[existing] + separator + value
                    merged = True
                    break
            if not merged:
                headers[key] = value

        forwarded_host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
        headers["Host"] = forwarded_host
        headers.setdefault("X-Forwarded-Host", forwarded_host)
        # Always override: the router's value reflects its own inbound
        # (plain-HTTP) leg, not what the visitor's browser used.
        headers["X-Forwarded-Proto"] = EXTERNAL_SCHEME
        client_ip = self.client_address[0]
        if not self.headers.get("X-Forwarded-For"):
            headers["X-Forwarded-For"] = client_ip
        headers["Connection"] = "close"
        if body or self.command in ("POST", "PUT", "PATCH", "DELETE"):
            headers["Content-Length"] = str(len(body))
        return headers

    def _forward(self, body: bytes, upstream_headers: dict):
        # BaseHTTPRequestHandler decodes the request line as iso-8859-1, so
        # self.path can carry raw 0x80-0xFF code points; http.client would
        # crash encoding them as ASCII. Percent-encode anything non-ASCII
        # ("%" kept safe so existing escapes pass through untouched).
        target = urllib.parse.quote(
            self.path, safe="!#$%&'()*+,/:;=?@[]~", encoding="iso-8859-1"
        )
        connection = http.client.HTTPConnection(
            UPSTREAM_HOST, UPSTREAM_PORT, timeout=UPSTREAM_TIMEOUT_SECONDS
        )
        try:
            connection.request(self.command, target, body=body, headers=upstream_headers)
            response = connection.getresponse()
        except (http.client.HTTPException, socket.timeout, OSError, UnicodeError):
            # Covers connection-refused (cold start), timeouts, and
            # malformed upstream responses (BadStatusLine etc.).
            connection.close()
            self._respond_upstream_down()
            return

        try:
            self.send_response(response.status, response.reason)
            has_length = False
            for key, value in response.getheaders():
                lower = key.lower()
                if lower in HOP_BY_HOP_HEADERS:
                    continue
                if lower == "content-length":
                    has_length = True
                self.send_header(key, value)
            if not has_length:
                # We stream until upstream EOF; signal framing via close.
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command != "HEAD" and response.status not in (204, 304):
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        finally:
            connection.close()

    def _respond_upstream_down(self):
        path = self.path.split("?", 1)[0]
        if self.command in ("GET", "HEAD") and path == "/":
            # Keep OpenHost's readiness probe (GET /, accepts <500) happy
            # while Keycloak finishes its cold start.
            self._send_simple(200, PLACEHOLDER_HTML, "text/html; charset=utf-8")
        else:
            self._send_simple(503, b"Keycloak is starting; retry shortly\n", extra_headers={"Retry-After": "5"})


def main():
    threading.Thread(target=cleanup_stale_sso_users, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), KeycloakProxyHandler)
    server.daemon_threads = True
    print(
        "[proxy] listening on 0.0.0.0:%d -> %s:%d" % (LISTEN_PORT, UPSTREAM_HOST, UPSTREAM_PORT),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()

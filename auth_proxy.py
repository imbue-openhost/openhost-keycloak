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

3. Header hygiene: rewrites ``Host`` from ``X-Forwarded-Host`` (Keycloak
   builds issuer/redirect URLs from forwarded headers), guarantees
   ``X-Forwarded-Proto`` is set, and strips ``X-OpenHost-*`` headers
   before forwarding upstream.

4. Basic abuse hardening: request body size cap and client socket
   timeouts (this port serves anonymous internet traffic).

Stdlib only; no third-party dependencies.
"""

import http.client
import http.server
import os
import posixpath
import socket
import sys
import urllib.parse

LISTEN_PORT = int(os.environ.get("PROXY_LISTEN_PORT", "8080"))
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = int(os.environ.get("KC_UPSTREAM_PORT", "8081"))

MAX_BODY_BYTES = 64 * 1024 * 1024  # generous; realm imports can be large
CLIENT_TIMEOUT_SECONDS = 60
UPSTREAM_TIMEOUT_SECONDS = 120

OWNER_HEADER = "X-OpenHost-Is-Owner"

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
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
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
        if self.path.split("?", 1)[0] == "/_healthz":
            self._send_simple(200, b"ok\n")
            return

        is_owner = (self.headers.get(OWNER_HEADER) or "").strip().lower() == "true"
        if is_owner_only(self.path) and not is_owner:
            # 404 (not 403) so the admin surface is indistinguishable from
            # a missing route for anonymous scanners.
            self._send_simple(404, b"not found\n")
            return

        body, error = self._read_body()
        if error:
            return

        upstream_headers = self._build_upstream_headers(body)
        self._forward(body, upstream_headers)

    def _read_body(self):
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
        if length > MAX_BODY_BYTES:
            self._send_simple(413, b"request body too large\n")
            return None, True
        try:
            body = self.rfile.read(length)
        except socket.timeout:
            self._send_simple(408, b"timed out reading request body\n")
            return None, True
        if len(body) != length:
            # Short read; client went away or lied about length.
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
            # Collapse duplicates with a comma per RFC 9110.
            if key in headers or lower in (existing.lower() for existing in headers):
                for existing in list(headers):
                    if existing.lower() == lower:
                        headers[existing] = headers[existing] + ", " + value
                        break
            else:
                headers[key] = value

        forwarded_host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
        headers["Host"] = forwarded_host
        headers.setdefault("X-Forwarded-Host", forwarded_host)
        if not self.headers.get("X-Forwarded-Proto"):
            headers["X-Forwarded-Proto"] = "https"
        client_ip = self.client_address[0]
        if not self.headers.get("X-Forwarded-For"):
            headers["X-Forwarded-For"] = client_ip
        headers["Connection"] = "close"
        if body or self.command in ("POST", "PUT", "PATCH", "DELETE"):
            headers["Content-Length"] = str(len(body))
        return headers

    def _forward(self, body: bytes, upstream_headers: dict):
        connection = http.client.HTTPConnection(
            UPSTREAM_HOST, UPSTREAM_PORT, timeout=UPSTREAM_TIMEOUT_SECONDS
        )
        try:
            connection.request(self.command, self.path, body=body, headers=upstream_headers)
            response = connection.getresponse()
        except (ConnectionRefusedError, socket.timeout, OSError):
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
    server = http.server.ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), KeycloakProxyHandler)
    server.daemon_threads = True
    print(
        "[proxy] listening on 0.0.0.0:%d -> %s:%d" % (LISTEN_PORT, UPSTREAM_HOST, UPSTREAM_PORT),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()

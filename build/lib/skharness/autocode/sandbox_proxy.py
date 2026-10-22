"""Sovereign CONNECT allowlist proxy: the sole egress from the sandbox network.
Allows a CONNECT only to an exact host in the pinned allowlist; everything else
gets 403. Stdlib only, fully inspectable."""
from __future__ import annotations

import http.client
import http.server
import select
import socket
import urllib.parse

_HOP_BY_HOP = {"proxy-connection", "connection"}
# Response framing headers the proxy must NOT copy through: it buffers the full
# upstream body (resp.read() de-chunks it), so forwarding the upstream's
# `Transfer-Encoding: chunked` or original `Content-Length` would describe a body
# that no longer matches what we write, and a strict client (undici/opencode) then
# rejects it as InvalidHTTPResponse. We drop these and set Content-Length ourselves.
_RESP_FRAMING = {"transfer-encoding", "content-length", "keep-alive"}


class _RequestHeaders(dict):
    """An outbound header dict whose field names compare case-insensitively.

    HTTP field names are case-insensitive, but a plain dict's keys are not. The
    outbound headers here are seeded from the client's request, which preserves
    the client's original casing, and then some fields are overridden. With a
    plain dict a client that sent `host:` and an override written as `Host` are
    two DISTINCT keys, so both go on the wire. RFC 7230 section 5.4 says a
    server MUST reject a request with more than one Host field as 400, and the
    only reason that has not bitten yet is that skgateway tolerates it.

    Keying case-insensitively makes that whole class of bug impossible rather
    than special-casing Host: any field this proxy sets replaces the client's
    field whatever case it arrived in, today and for whatever gets overridden
    next. The casing of the most recent assignment is what goes on the wire.
    """

    def __init__(self, items=()):
        super().__init__()
        for key, value in items:
            self[key] = value

    def _existing(self, key: str):
        lowered = key.lower()
        for present in super().keys():
            if present.lower() == lowered:
                return present
        return None

    def __setitem__(self, key, value):
        present = self._existing(key)
        if present is not None and present != key:
            super().__delitem__(present)
        super().__setitem__(key, value)

    def __getitem__(self, key):
        present = self._existing(key)
        if present is None:
            raise KeyError(key)
        return super().__getitem__(present)

    def __delitem__(self, key):
        present = self._existing(key)
        if present is None:
            raise KeyError(key)
        super().__delitem__(present)

    def __contains__(self, key):
        return isinstance(key, str) and self._existing(key) is not None

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def update(self, other=(), **kwargs):
        items = other.items() if hasattr(other, "items") else other
        for key, value in items:
            self[key] = value
        for key, value in kwargs.items():
            self[key] = value


class AllowlistProxy:
    def __init__(self, allow: list[str]) -> None:
        self.allow = {h.strip().lower() for h in allow if h and h.strip()}

    def is_allowed(self, host: str) -> bool:
        if not host:
            return False
        return host.strip().lower().split(":", 1)[0] in self.allow


def _target_host(path: str) -> str:
    """Return the hostname of an absolute http(s) request URI, or "" if
    the path is relative (not a forward-proxy request)."""
    if not (path.startswith("http://") or path.startswith("https://")):
        return ""
    parsed = urllib.parse.urlsplit(path)
    return parsed.hostname or ""


def _handler(proxy: AllowlistProxy, log):
    class H(http.server.BaseHTTPRequestHandler):
        def _forward(self):
            host = _target_host(self.path)
            if not proxy.is_allowed(host):
                if log:
                    log(f"DENY {self.command} {self.path}")
                self.send_error(403, "egress denied")
                return
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.scheme == "https":
                # This proxy originates no TLS: it only ever built a cleartext
                # http.client.HTTPConnection, so forwarding an https absolute-URI
                # would have sent the request (Authorization header and all) in the
                # clear, by default to port 80. It is a confinement boundary, so it
                # fails closed rather than silently downgrading. https reaches an
                # origin through CONNECT, which takes the blind tunnel path below
                # and leaves TLS end to end between the client and the origin.
                if log:
                    log(f"REFUSE {self.command} {host} (https absolute-URI, "
                        f"no TLS origination; use CONNECT)")
                self.send_error(
                    501, "https forwarding unsupported",
                    "This proxy does not originate TLS. Use CONNECT for https so "
                    "the tunnel stays end to end; it will not be downgraded to "
                    "cleartext.")
                return
            if log:
                log(f"ALLOW {self.command} {host}")

            port = parsed.port or 80
            target = f"{parsed.path or '/'}"
            if parsed.query:
                target = f"{target}?{parsed.query}"

            content_length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(content_length) if content_length else None

            headers = _RequestHeaders(
                (k, v)
                for k, v in self.headers.items()
                if k.lower() not in _HOP_BY_HOP
            )
            headers["Host"] = parsed.netloc

            try:
                upstream = http.client.HTTPConnection(parsed.hostname, port, timeout=30)
                upstream.request(self.command, target, body=body, headers=headers)
                resp = upstream.getresponse()
                resp_body = resp.read()
            except OSError:
                self.send_error(502, "upstream unreachable")
                return

            self.send_response(resp.status)
            for name, value in resp.getheaders():
                if name.lower() in _HOP_BY_HOP or name.lower() in _RESP_FRAMING:
                    continue
                self.send_header(name, value)
            # we buffered the whole body; describe exactly what we write so a strict
            # client parses a well-formed HTTP/1.1 message (see _RESP_FRAMING).
            self.send_header("Content-Length", str(len(resp_body or b"")))
            self.end_headers()
            if resp_body:
                self.wfile.write(resp_body)
            upstream.close()

        def do_GET(self):                            # noqa: N802
            self._forward()

        def do_POST(self):                           # noqa: N802
            self._forward()

        def do_PUT(self):                            # noqa: N802
            self._forward()

        def do_DELETE(self):                         # noqa: N802
            self._forward()

        def do_PATCH(self):                          # noqa: N802
            self._forward()

        def do_HEAD(self):                           # noqa: N802
            self._forward()

        def do_CONNECT(self):                       # noqa: N802
            host = self.path.split(":", 1)[0]
            if not proxy.is_allowed(host):
                if log:
                    log(f"DENY {self.path}")
                self.send_error(403, "egress denied")
                return
            if log:
                log(f"ALLOW {self.path}")
            hostname, _, port = self.path.partition(":")
            try:
                upstream = socket.create_connection((hostname, int(port or 443)), timeout=30)
            except OSError:
                self.send_error(502, "upstream unreachable")
                return
            self.send_response(200, "Connection Established")
            self.end_headers()
            self._tunnel(self.connection, upstream)

        def _tunnel(self, a, b):
            socks = [a, b]
            while True:
                r, _, x = select.select(socks, [], socks, 60)
                if x or not r:
                    break
                for s in r:
                    other = b if s is a else a
                    data = s.recv(65536)
                    if not data:
                        return
                    other.sendall(data)

        def log_message(self, *a):                  # silence default logging
            return
    return H


def serve(allow: list[str], port: int = 8080, log=None) -> None:
    proxy = AllowlistProxy(allow)
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), _handler(proxy, log))
    httpd.serve_forever()


if __name__ == "__main__":                          # python -m ... <port> <host> ...
    import sys
    serve(sys.argv[2:], int(sys.argv[1]))

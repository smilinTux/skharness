import http.client
import socket
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from skharness.autocode.sandbox_proxy import (
    AllowlistProxy,
    _handler,
    _target_host,
    serve,
)


def test_target_host_from_absolute_http_uri():
    assert _target_host("http://172.17.0.1:18780/v1/chat") == "172.17.0.1"


def test_target_host_from_absolute_http_uri_no_port():
    assert _target_host("http://gw.local/x") == "gw.local"


def test_target_host_from_relative_path_is_empty():
    assert _target_host("/relative") == ""


def test_target_host_feeds_allowlist_check():
    assert AllowlistProxy(["172.17.0.1"]).is_allowed(_target_host("http://172.17.0.1:18780/v1")) is True


def test_allows_only_listed_hosts():
    p = AllowlistProxy(["github.com", "gw.local"])
    assert p.is_allowed("github.com") is True
    assert p.is_allowed("GITHUB.COM") is True          # case-insensitive
    assert p.is_allowed("github.com:443") is True       # port stripped
    assert p.is_allowed("evil.example.com") is False
    assert p.is_allowed("") is False
    assert p.is_allowed("githubXcom") is False          # no substring match


def test_empty_allowlist_denies_all():
    assert AllowlistProxy([]).is_allowed("github.com") is False


class _ChunkedUpstream(BaseHTTPRequestHandler):
    """Fake upstream that answers with Transfer-Encoding: chunked, like skgateway
    streaming does. The proxy buffers + de-chunks the body, so it must NOT pass the
    chunked header through (that mismatch is what broke opencode/undici)."""

    def do_POST(self):                                # noqa: N802
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body))

    def log_message(self, *a):                        # silence
        return


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_forward_restrips_chunked_framing_and_sets_content_length():
    up_port, px_port = _free_port(), _free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", up_port), _ChunkedUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    threading.Thread(
        target=serve, args=(["127.0.0.1"], px_port), daemon=True).start()

    import time
    time.sleep(0.3)                                   # let both servers bind
    conn = http.client.HTTPConnection("127.0.0.1", px_port, timeout=5)
    # forward-proxy request: absolute-form URI with the (allowlisted) upstream host
    conn.request("POST", f"http://127.0.0.1:{up_port}/v1/chat", body=b"{}",
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    payload = resp.read()

    assert resp.status == 200
    assert payload == b'{"ok":true}'
    # the chunked framing must be gone; a concrete Content-Length must describe the body
    assert resp.getheader("Transfer-Encoding") is None
    assert resp.getheader("Content-Length") == str(len(payload))
    upstream.shutdown()


# --------------------------------------------------------------------------
# On-the-wire capture: an origin that records the RAW request bytes.
#
# http.server's own parser folds duplicate headers away, so a fake upstream
# built on BaseHTTPRequestHandler cannot see a double Host. These tests speak
# to a bare socket instead and assert on the exact bytes the proxy emitted.
# --------------------------------------------------------------------------


class _RawOrigin:
    """A one-shot socket origin that records raw request bytes."""

    def __init__(self):
        self.requests: list[bytes] = []
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))          # port 0: never collides with a
        self._sock.listen(8)                        # parallel test run
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(5)
        buf = b""
        try:
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value.strip() or b"0")
            while len(rest) < length:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                rest += chunk
            self.requests.append(head + b"\r\n\r\n" + rest)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                b"Content-Length: 2\r\n\r\nok"
            )
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()

    def header_lines(self, name: str) -> list[bytes]:
        """Every raw header line of `name` across every captured request."""
        want = name.lower().encode()
        out = []
        for raw in self.requests:
            head = raw.split(b"\r\n\r\n", 1)[0]
            for line in head.split(b"\r\n")[1:]:
                if line.split(b":", 1)[0].strip().lower() == want:
                    out.append(line)
        return out


class _RunningProxy:
    """The real proxy on an ephemeral port, with a log so requests are observable."""

    def __init__(self, allow):
        self.log: list[str] = []
        self._httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), _handler(AllowlistProxy(allow), self.log.append))
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)


def test_forwarded_request_carries_exactly_one_host_header():
    """DEFECT 1: the outbound header dict was copied with the CLIENT's casing, so a
    client that sent `host:` kept that key while the proxy set `Host` as a second,
    distinct key. Both went on the wire. RFC 7230 makes a multi-Host request a MUST
    reject / 400, and only skgateway's tolerance hid it. Proven at the origin, on
    the bytes, not by reading the source."""
    origin = _RawOrigin()
    proxy = _RunningProxy(["127.0.0.1"])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        # lowercase `host` also tells http.client to skip its own Host header, so
        # exactly one Host leaves the CLIENT. Anything extra was added by the proxy.
        conn.request(
            "POST",
            f"http://127.0.0.1:{origin.port}/v1/chat",
            body=b"{}",
            headers={"host": f"127.0.0.1:{origin.port}",
                     "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 200

        assert len(origin.requests) == 1, "origin saw no request"
        hosts = origin.header_lines("host")
        assert len(hosts) == 1, f"expected exactly one Host header, got {hosts}"
        assert hosts[0].split(b":", 1)[1].strip().startswith(b"127.0.0.1")
    finally:
        proxy.close()
        origin.close()


def test_forwarded_request_host_override_survives_odd_client_casing():
    """The fix must be structural, not a Host special-case: ANY header the proxy
    overrides has to replace the client's field whatever case it arrived in."""
    origin = _RawOrigin()
    proxy = _RunningProxy(["127.0.0.1"])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        conn.request(
            "GET",
            f"http://127.0.0.1:{origin.port}/x",
            headers={"hOsT": "spoofed.example.com"},
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 200

        hosts = origin.header_lines("host")
        assert len(hosts) == 1, f"expected exactly one Host header, got {hosts}"
        # and it is the proxy's value (the real target), not the client's spoof
        assert b"spoofed.example.com" not in hosts[0]
    finally:
        proxy.close()
        origin.close()


def test_https_absolute_uri_is_never_downgraded_to_cleartext():
    """DEFECT 2: _target_host ACCEPTS https:// absolute URIs but _forward only ever
    built an http.client.HTTPConnection on `parsed.port or 80`, so an https request
    went out as CLEARTEXT. This proxy terminates no TLS (https normally arrives as
    CONNECT and takes the blind tunnel path), so the only safe answer is to refuse.
    Proven by watching a plaintext origin: it must receive NOTHING."""
    origin = _RawOrigin()
    proxy = _RunningProxy(["127.0.0.1"])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        conn.request("GET", f"https://127.0.0.1:{origin.port}/secret",
                     headers={"Authorization": "Bearer sk-not-for-the-wire"})
        resp = conn.getresponse()
        resp.read()

        # refused, never proxied
        assert resp.status == 501, f"expected a refusal, got {resp.status}"
        assert origin.requests == [], (
            "https request was sent to a plaintext origin: "
            f"{origin.requests!r}"
        )
        assert any("https" in line.lower() for line in proxy.log), proxy.log
    finally:
        proxy.close()
        origin.close()


def test_https_absolute_uri_without_port_is_refused_not_sent_to_80():
    """Same defect, the shape that is easiest to hit by accident: no explicit port,
    so `parsed.port or 80` silently picked plaintext 80."""
    proxy = _RunningProxy(["gw.local"])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        conn.request("GET", "https://gw.local/v1/chat")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 501, f"expected a refusal, got {resp.status}"
    finally:
        proxy.close()


def test_http_absolute_uri_still_forwards_after_the_tls_guard():
    """Negative control for the guard above: plain http, the only shape today's
    skgateway flow uses, must still be proxied."""
    origin = _RawOrigin()
    proxy = _RunningProxy(["127.0.0.1"])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        conn.request("GET", f"http://127.0.0.1:{origin.port}/v1/models")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.read() == b"ok"
        assert len(origin.requests) == 1
    finally:
        proxy.close()
        origin.close()


def test_denied_host_is_still_403_not_the_tls_refusal():
    """A non-allowlisted host must keep its DENY signal, whatever the scheme: the
    allowlist check runs first so 403 never degrades into 501."""
    proxy = _RunningProxy(["gw.local"])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        conn.request("GET", "https://evil.example.com/x")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 403
        assert any(line.startswith("DENY") for line in proxy.log), proxy.log
    finally:
        proxy.close()

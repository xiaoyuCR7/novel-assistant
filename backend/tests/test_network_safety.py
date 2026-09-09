import socket

import httpcore
import pytest

from novel_harness.ai import network_safety as safety
from novel_harness.ai.base import ProviderExecutionError


def test_dns_private_and_mixed_answers_are_rejected_before_connect(monkeypatch):
    def answers(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 443))
            for host in ["8.8.8.8", "127.0.0.1"]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", answers)
    monkeypatch.setattr(socket, "socket", lambda *a: pytest.fail("Must not connect"))
    with pytest.raises(ProviderExecutionError, match="目标"):
        safety.PinnedBackend(30).connect_tcp("service.example", 443)


def test_connection_uses_validated_ip_without_second_hostname_lookup(monkeypatch):
    lookups, targets, tls_names = [], [], []

    def resolve(host, *args, **kwargs):
        lookups.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    class Socket:
        def setsockopt(self, *args):
            pass

        def settimeout(self, value):
            pass

        def connect(self, target):
            targets.append(target)

        def getpeername(self):
            return ("8.8.8.8", 443)

        def close(self):
            pass

    class TLS:
        def wrap_socket(self, sock, server_hostname):
            tls_names.append(server_hostname)
            return sock

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", lambda *a: Socket())
    stream = safety.PinnedBackend(30).connect_tcp("service.example", 443)
    stream.start_tls(TLS(), "service.example", 10)
    assert lookups == ["service.example"]
    assert targets == [("8.8.8.8", 443)]
    assert tls_names == ["service.example"]


def test_total_deadline_is_not_reset_by_progress(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(safety, "monotonic", lambda: now[0])
    backend = safety.PinnedBackend(5)
    now[0] = 6
    with pytest.raises(httpcore.ReadTimeout):
        backend.remaining(None)


@pytest.mark.parametrize("operation", ["connect", "tls"])
def test_deadline_exception_does_not_leak_socket(monkeypatch, operation):
    closed = []

    class Socket:
        def settimeout(self, value):
            pass

        def close(self):
            closed.append(True)

    backend = safety.PinnedBackend(30)
    monkeypatch.setattr(socket, "socket", lambda *a: Socket())

    def expired(_):
        raise httpcore.ReadTimeout("expired")

    monkeypatch.setattr(backend, "remaining", expired)
    with pytest.raises(httpcore.ReadTimeout):
        if operation == "connect":
            backend.connect_tcp("127.0.0.1", 1234)
        else:
            safety.DeadlineStream(Socket(), backend).start_tls(None)
    assert closed == [True]


def test_real_loopback_transport_preserves_request_and_response():
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from novel_harness.ai.base import AITextRequest
    from novel_harness.ai.local_provider import LocalProvider

    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            assert self.headers["Accept-Encoding"] == "identity"
            body = json.dumps({"message": {"content": "synthetic ok"}, "done": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            result = LocalProvider(
                f"http://127.0.0.1:{server.server_port}", "synthetic"
            ).generate_text(
                AITextRequest(task="chat", developer_instruction="write", user_prompt="synthetic")
            )
            assert result.text == "synthetic ok"
            assert received[0]["model"] == "synthetic"
        finally:
            server.shutdown()
            thread.join(timeout=2)

"""Validated numeric connections with original Host/TLS identity and total deadline."""

import ipaddress
import socket
import ssl
from contextlib import contextmanager
from queue import Empty, Queue
from threading import BoundedSemaphore, Thread
from time import monotonic

import httpcore
import httpx

from novel_harness.ai.base import ProviderExecutionError

_dns_slots = BoundedSemaphore(2)


@contextmanager
def mapped_errors():
    try:
        yield
    except httpcore.ConnectTimeout:
        raise httpx.ConnectTimeout("Connection timeout") from None
    except httpcore.ConnectError:
        raise httpx.ConnectError("Connection failed") from None
    except httpcore.TimeoutException:
        raise httpx.ReadTimeout("Stage deadline exceeded") from None
    except (httpcore.NetworkError, httpcore.ProtocolError):
        raise httpx.RemoteProtocolError("Network failure") from None


class PinnedBackend(httpcore.NetworkBackend):
    def __init__(self, seconds):
        self.deadline = monotonic() + seconds

    def remaining(self, timeout):
        left = self.deadline - monotonic()
        if left <= 0:
            raise httpcore.ReadTimeout("Stage deadline exceeded")
        return min(left, timeout) if timeout is not None else left

    def _addresses(self, host, port):
        if host == "localhost":
            host = "127.0.0.1"
        try:
            address = ipaddress.ip_address(host)
            addresses = [
                (socket.AF_INET6 if address.version == 6 else socket.AF_INET, str(address))
            ]
            allow_loopback = address.is_loopback
        except ValueError:
            allow_loopback = False
            if not _dns_slots.acquire(timeout=self.remaining(None)):
                raise httpcore.ConnectTimeout("DNS busy") from None
            result = Queue(maxsize=1)

            def resolve():
                try:
                    result.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
                except OSError:
                    result.put(None)
                finally:
                    _dns_slots.release()

            Thread(target=resolve, daemon=True, name="novel-dns").start()
            try:
                rows = result.get(timeout=self.remaining(None))
            except Empty:
                raise httpcore.ConnectTimeout("DNS timeout") from None
            if not rows:
                raise httpcore.ConnectError("DNS failed") from None
            addresses = list(dict.fromkeys((row[0], row[4][0]) for row in rows))
        if not addresses or any(
            not (
                ipaddress.ip_address(ip).is_global
                or (allow_loopback and ipaddress.ip_address(ip).is_loopback)
            )
            for _, ip in addresses
        ):
            raise ProviderExecutionError(
                "API 目标解析到不允许的网络地址。", outcome="known", code="UNSAFE_NETWORK_TARGET"
            )
        return addresses

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = self._addresses(host, port)
        for family, ip in addresses:
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                sock.settimeout(self.remaining(timeout))
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                for option in socket_options or []:
                    sock.setsockopt(*option)
                if local_address:
                    sock.bind((local_address, 0))
                sock.connect((ip, port))  # Numeric socket.connect cannot re-resolve the hostname.
                if ipaddress.ip_address(sock.getpeername()[0]) != ipaddress.ip_address(ip):
                    raise OSError("Peer mismatch")
                return DeadlineStream(sock, self)
            except OSError:
                sock.close()
            except BaseException:
                sock.close()
                raise
        raise httpcore.ConnectError("Connection failed")


class DeadlineStream(httpcore.NetworkStream):
    def __init__(self, sock, backend):
        self.sock, self.backend = sock, backend

    def read(self, max_bytes, timeout=None):
        try:
            self.sock.settimeout(self.backend.remaining(timeout))
            return self.sock.recv(max_bytes)
        except TimeoutError:
            raise httpcore.ReadTimeout("Deadline") from None
        except OSError:
            raise httpcore.ReadError("Read failed") from None

    def write(self, buffer, timeout=None):
        try:
            while buffer:
                self.sock.settimeout(self.backend.remaining(timeout))
                size = self.sock.send(buffer[:65536])
                if not size:
                    raise OSError("Closed")
                buffer = buffer[size:]
        except TimeoutError:
            raise httpcore.WriteTimeout("Deadline") from None
        except OSError:
            raise httpcore.WriteError("Write failed") from None

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        try:
            self.sock.settimeout(self.backend.remaining(timeout))
            return DeadlineStream(
                ssl_context.wrap_socket(self.sock, server_hostname=server_hostname), self.backend
            )
        except OSError:
            self.close()
            raise httpcore.ConnectError("TLS failed") from None
        except BaseException:
            self.close()
            raise

    def close(self):
        self.sock.close()

    def get_extra_info(self, info):
        if info == "socket":
            return self.sock
        if info == "server_addr":
            return self.sock.getpeername()
        if info == "ssl_object" and isinstance(self.sock, ssl.SSLSocket):
            return self.sock
        return None


class ResponseStream(httpx.SyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        with mapped_errors():
            yield from self.stream

    def close(self):
        self.stream.close()


class SafeTransport(httpx.BaseTransport):
    def __init__(self, seconds=180):
        self.pool = httpcore.ConnectionPool(network_backend=PinnedBackend(seconds), retries=0)

    def handle_request(self, request):
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with mapped_errors():
            response = self.pool.handle_request(core_request)
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=ResponseStream(response.stream),
            extensions=response.extensions,
        )

    def close(self):
        self.pool.close()

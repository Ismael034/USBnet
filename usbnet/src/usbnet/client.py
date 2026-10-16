from __future__ import annotations

import collections
import math
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .bulk import open_bulk
from .feature import open_feature
from .protocol import (
    CLOSE,
    CREDIT,
    CREDIT_STEP,
    DATA,
    HELLO,
    OPEN,
    OPENED,
    PIECE,
    WINDOW,
    FrameReassembler,
    decode_credit,
    decode_message,
    encode_credit,
    encode_message,
    encode_open,
    read_frames,
    write_frames,
)

DEFAULT_PORT = 8080
DEFAULT_TIMEOUT = 30.0
BUSY_DECAY = 0.5  # seconds; recently busy tunnels yield to quieter ones
SOCKET_CHUNK = 65536


def open_client_device():
    """Prefer the bulk interface, else the HID tunnel feature report."""
    errors = []
    for opener in (lambda: open_bulk("client"), open_feature):
        try:
            return opener()
        except Exception as error:  # noqa: BLE001 - try the next transport
            errors.append(str(error))
    raise OSError("no USBnet Client found: " + "; ".join(errors))


class Tunnel:
    def __init__(self, tunnel_id: int, lock: threading.Lock):
        self.id = tunnel_id
        self.cond = threading.Condition(lock)
        self.opened = False
        self.closed = False        # the server closed it, or it failed
        self.local_closed = False  # the browser side is done sending
        self.error = None
        self.incoming = collections.deque()
        self.outgoing = bytearray()
        self.window = WINDOW       # bytes the server still accepts
        self.consumed = 0          # delivered bytes not yet credited back
        self.busy = 0.0
        self.stamp = time.monotonic()

    def score(self, now: float) -> float:
        return self.busy * math.exp((self.stamp - now) / BUSY_DECAY)

    def charge(self, size: int) -> None:
        now = time.monotonic()
        self.busy = self.score(now) + size
        self.stamp = now


class StreamLink:
    """Multiplex tunnels over one USB device as a continuous message stream."""

    def __init__(self, dev):
        self.dev = dev
        self.piece = getattr(dev, "piece", PIECE)
        self._lock = threading.Lock()
        self._work = threading.Event()
        self._tunnels: dict[int, Tunnel] = {}
        self._control = collections.deque([encode_message(HELLO)])
        self._next_id = 1
        self._running = True
        self._incoming = FrameReassembler()
        self._last_activity = time.monotonic()
        if getattr(dev, "duplex", False):
            loops = (self._read_loop, self._write_loop)
        else:
            loops = (self._pump,)
        self._threads = [threading.Thread(target=loop, daemon=True) for loop in loops]
        for thread in self._threads:
            thread.start()

    # Tunnel API used by the proxy handlers.

    def open(self, host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> Tunnel:
        with self._lock:
            tunnel = Tunnel(self._next_id, self._lock)
            self._next_id += 1
            self._tunnels[tunnel.id] = tunnel
            self._control.append(encode_message(OPEN, tunnel.id, encode_open(host, port)))
        self._work.set()
        with tunnel.cond:
            tunnel.cond.wait_for(lambda: tunnel.opened or tunnel.closed, timeout)
            failed = not tunnel.opened
        if failed:
            self.close(tunnel)
            raise OSError(tunnel.error or "tunnel open timed out")
        return tunnel

    def send(self, tunnel: Tunnel, data: bytes) -> None:
        with tunnel.cond:
            tunnel.cond.wait_for(lambda: tunnel.closed or len(tunnel.outgoing) < WINDOW)
            if tunnel.closed:
                raise OSError(tunnel.error or "tunnel closed")
            tunnel.outgoing.extend(data)
        self._work.set()

    def recv(self, tunnel: Tunnel) -> bytes | None:
        """Return received bytes, or None once the tunnel is closed and drained."""
        with tunnel.cond:
            tunnel.cond.wait_for(lambda: tunnel.incoming or tunnel.closed)
            if tunnel.incoming:
                data = b"".join(tunnel.incoming)
                tunnel.incoming.clear()
                return data
            return None

    def consumed(self, tunnel: Tunnel, size: int) -> None:
        with self._lock:
            tunnel.consumed += size
            if tunnel.consumed >= CREDIT_STEP and tunnel.id in self._tunnels:
                self._control.append(encode_message(CREDIT, tunnel.id, encode_credit(tunnel.consumed)))
                tunnel.consumed = 0
        self._work.set()

    def close(self, tunnel: Tunnel) -> None:
        """Close after the queued outgoing bytes are sent."""
        with self._lock:
            tunnel.local_closed = True
            if tunnel.id in self._tunnels and not tunnel.outgoing:
                del self._tunnels[tunnel.id]
                self._control.append(encode_message(CLOSE, tunnel.id))
            tunnel.closed = True
            tunnel.cond.notify_all()
        self._work.set()

    def stop(self) -> None:
        self._running = False
        self._work.set()
        with self._lock:
            for tunnel in self._tunnels.values():
                tunnel.closed = True
                tunnel.error = "tunnel link stopped"
                tunnel.cond.notify_all()

    # USB side.

    def _next_message(self) -> tuple[bytes | None, bool]:
        with self._lock:
            if self._control:
                message = self._control.popleft()
                return message, message[0] in (CREDIT, HELLO)
            now = time.monotonic()
            ready = [tunnel for tunnel in self._tunnels.values() if tunnel.outgoing and tunnel.window > 0]
            if not ready:
                return None, False
            tunnel = min(ready, key=lambda item: item.score(now))
            size = min(self.piece, tunnel.window, len(tunnel.outgoing))
            piece = bytes(tunnel.outgoing[:size])
            del tunnel.outgoing[:size]
            tunnel.window -= size
            tunnel.charge(size)
            tunnel.cond.notify_all()
            if tunnel.local_closed and not tunnel.outgoing:
                del self._tunnels[tunnel.id]
                self._control.append(encode_message(CLOSE, tunnel.id))
            return encode_message(DATA, tunnel.id, piece), False

    def _write(self, message: bytes, quiet: bool) -> None:
        try:
            write_frames(self.dev, message, quiet=quiet)
        except (OSError, TimeoutError) as error:
            print(f"USB write failed: {error}", file=sys.stderr)

    def _dispatch(self, frames) -> None:
        for frame in frames:
            try:
                raw = self._incoming.push(frame)
                if raw is None:
                    continue
                kind, tunnel_id, payload = decode_message(raw)
            except ValueError:
                continue
            with self._lock:
                tunnel = self._tunnels.get(tunnel_id)
                if tunnel is None:
                    continue
                if kind == OPENED:
                    tunnel.opened = True
                elif kind == DATA:
                    tunnel.incoming.append(payload)
                elif kind == CLOSE:
                    tunnel.closed = True
                    tunnel.error = payload.decode("utf-8", "replace") or None
                    del self._tunnels[tunnel_id]
                elif kind == CREDIT:
                    tunnel.window += decode_credit(payload)
                    self._work.set()
                tunnel.cond.notify_all()

    def _pump(self) -> None:
        """Half-duplex devices (HID): alternate one write and one read."""
        while self._running:
            message, quiet = self._next_message()
            if message is not None:
                self._write(message, quiet)
            frames = read_frames(self.dev, 0)
            self._dispatch(frames)
            now = time.monotonic()
            if message is not None or frames:
                self._last_activity = now
                continue
            idle = now - self._last_activity
            # Poll fast while replies are likely, slower once the link is idle.
            delay = 0.0002 if idle < 0.25 else 0.002 if idle < 5 else 0.01
            self._work.wait(delay)
            self._work.clear()

    def _read_loop(self) -> None:
        while self._running:
            try:
                self._dispatch(read_frames(self.dev, 100))
            except (OSError, TimeoutError) as error:
                print(f"USB read failed: {error}", file=sys.stderr)
                time.sleep(0.1)

    def _write_loop(self) -> None:
        while self._running:
            self._work.clear()
            message, quiet = self._next_message()
            if message is None:
                self._work.wait(0.5)
            else:
                self._write(message, quiet)


def make_proxy_handler(link: StreamLink):
    """Expose the Mac's TCP access as a local HTTP proxy."""

    class ProxyHandler(BaseHTTPRequestHandler):
        rbufsize = 0  # do not buffer TLS bytes after the CONNECT request headers

        def proxy_error(self, error):
            try:
                self.send_error(502, str(error))
            except OSError:
                self.close_connection = True

        def deliver(self, tunnel: Tunnel) -> None:
            """Copy tunnel bytes to the browser until the tunnel closes."""
            try:
                while (data := link.recv(tunnel)) is not None:
                    self.connection.sendall(data)
                    link.consumed(tunnel, len(data))
            except OSError:
                pass
            finally:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        def do_CONNECT(self):
            try:
                target = urlsplit("//" + self.path)
                if not target.hostname or not target.port:
                    raise ValueError("CONNECT target must be host:port")
                tunnel = link.open(target.hostname, target.port)
            except (OSError, ValueError) as error:
                self.proxy_error(error)
                return
            self.send_response(200, "Connection Established")
            self.end_headers()
            self.close_connection = True
            self.connection.settimeout(None)
            downstream = threading.Thread(target=self.deliver, args=(tunnel,), daemon=True)
            downstream.start()
            try:
                while data := self.connection.recv(SOCKET_CHUNK):
                    link.send(tunnel, data)
            except OSError:
                pass
            finally:
                link.close(tunnel)
                downstream.join(DEFAULT_TIMEOUT)

        def forward_http(self):
            try:
                target = urlsplit(self.path)
                if target.scheme != "http" or not target.hostname:
                    raise ValueError("HTTP proxy request must contain an absolute URL")
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("chunked request uploads are unsupported")
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0:
                    raise ValueError("invalid Content-Length")
                path = target.path or "/"
                if target.query:
                    path += "?" + target.query
                header_lines = [f"{self.command} {path} HTTP/1.0",
                                f"Host: {target.netloc}", "Connection: close"]
                for key, value in self.headers.items():
                    if key.lower() not in {"host", "connection", "proxy-connection",
                                           "proxy-authorization", "transfer-encoding"}:
                        header_lines.append(f"{key}: {value}")
                tunnel = link.open(target.hostname, target.port or 80)
            except (OSError, ValueError) as error:
                self.proxy_error(error)
                return
            self.close_connection = True
            try:
                link.send(tunnel, "\r\n".join(header_lines).encode("latin-1") + b"\r\n\r\n")
                while length:
                    data = self.rfile.read(min(SOCKET_CHUNK, length))
                    if not data:
                        raise ValueError("request body ended before Content-Length")
                    length -= len(data)
                    link.send(tunnel, data)
                while (data := link.recv(tunnel)) is not None:
                    self.connection.sendall(data)
                    link.consumed(tunnel, len(data))
            except (OSError, ValueError) as error:
                print(f"HTTP {self.path}: {error}", file=sys.stderr)
            finally:
                link.close(tunnel)

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = forward_http

    return ProxyHandler


class ClientProxy:
    """Run the local HTTP proxy over one USBnet Client device."""

    def __init__(self, port: int = DEFAULT_PORT, device=None):
        if not 1 <= port <= 65535:
            raise ValueError("proxy port must be from 1 to 65535")
        self.port = port
        self.device = device
        self._owns_device = device is None

    def __enter__(self) -> "ClientProxy":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def serve_forever(self) -> None:
        if self.device is None:
            self.device = open_client_device()
        link = StreamLink(self.device)
        try:
            with ThreadingHTTPServer(("127.0.0.1", self.port), make_proxy_handler(link)) as httpd:
                httpd.daemon_threads = True
                print(f"USBnet TCP proxy listening at http://127.0.0.1:{self.port}", flush=True)
                httpd.serve_forever()
        finally:
            link.stop()
            self.close()

    def close(self) -> None:
        if self._owns_device and self.device is not None:
            self.device.close()
            self.device = None

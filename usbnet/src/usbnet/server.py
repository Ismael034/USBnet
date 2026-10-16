from __future__ import annotations

import collections
import concurrent.futures
import math
import select
import socket
import sys
import threading
import time

from .bulk import open_bulk
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
    decode_open,
    encode_credit,
    encode_message,
    read_frames,
    write_frames,
)

CONNECT_TIMEOUT = 15.0
MAX_TUNNELS = 256
BUSY_DECAY = 0.5  # seconds; recently busy tunnels yield to quieter ones


class Upstream:
    """One tunnel's TCP connection on the server side."""

    def __init__(self, tunnel: int):
        self.tunnel = tunnel
        self.sock = None
        self.open = False
        self.closing = False
        self.pending = bytearray()  # client bytes not yet sent upstream
        self.credit = WINDOW        # bytes we may still send to the client
        self.flushed = 0            # upstream bytes sent but not yet credited
        self.busy = 0.0
        self.stamp = time.monotonic()
        self.cancel = threading.Event()

    def charge(self, size: int) -> None:
        now = time.monotonic()
        self.busy = self.busy * math.exp((self.stamp - now) / BUSY_DECAY) + size
        self.stamp = now

    def score(self, now: float) -> float:
        return self.busy * math.exp((self.stamp - now) / BUSY_DECAY)


class USBnetServer:
    """Own the Server USB device and bridge its tunnels to TCP."""

    def __init__(self, device=None, connect_workers: int = 16):
        self.device = device
        self._owns_device = device is None
        self._piece = getattr(device, "piece", PIECE)
        self._lock = threading.Lock()
        self._tunnels: dict[int, Upstream] = {}
        self._control = collections.deque()
        self._generation = 0
        self._running = False
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._workers = concurrent.futures.ThreadPoolExecutor(max_workers=connect_workers)

    # Reader side: messages from the client.

    def _wake(self) -> None:
        try:
            self._wake_w.send(b"\0")
        except OSError:
            pass

    def handle_message(self, raw: bytes) -> None:
        kind, tunnel, payload = decode_message(raw)
        with self._lock:
            upstream = self._tunnels.get(tunnel)
            if kind == HELLO:
                self._generation += 1
                for item in self._tunnels.values():
                    item.closing = True
                self._control.clear()
            elif kind == OPEN:
                host, port = decode_open(payload)
                if upstream is not None or len(self._tunnels) >= MAX_TUNNELS:
                    self._control.append(encode_message(CLOSE, tunnel, b"too many tunnels"))
                else:
                    upstream = self._tunnels[tunnel] = Upstream(tunnel)
                    future = self._workers.submit(self._dial, host, port, upstream.cancel)
                    future.add_done_callback(lambda done, item=upstream: self._dialed(item, done))
            elif upstream is None:
                return  # stale message for a tunnel that is already gone
            elif kind == DATA:
                if len(upstream.pending) + len(payload) > WINDOW:
                    upstream.closing = True
                    self._control.append(encode_message(CLOSE, tunnel, b"tunnel window exceeded"))
                else:
                    upstream.pending.extend(payload)
            elif kind == CLOSE:
                upstream.closing = True
            elif kind == CREDIT:
                upstream.credit += decode_credit(payload)
        self._wake()

    @staticmethod
    def _dial(host, port, cancel):
        if cancel.is_set():
            return None
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        if cancel.is_set():
            sock.close()
            return None
        sock.setblocking(False)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock

    def _dialed(self, upstream: Upstream, future) -> None:
        try:
            sock = future.result()
            error = None if sock is not None else "connection cancelled"
        except OSError as failure:
            sock, error = None, str(failure) or "connect failed"
        with self._lock:
            current = self._tunnels.get(upstream.tunnel) is upstream and not upstream.closing
            if current and sock is not None:
                upstream.sock = sock
                upstream.open = True
                self._control.append(encode_message(OPENED, upstream.tunnel))
            else:
                if sock is not None:
                    sock.close()
                if current:
                    del self._tunnels[upstream.tunnel]
                    self._control.append(encode_message(CLOSE, upstream.tunnel, error.encode()))
        self._wake()

    def _read_loop(self) -> None:
        incoming = FrameReassembler()
        while self._running:
            try:
                frames = read_frames(self.device, 200)
            except OSError as error:
                print(f"USB read failed: {error}", file=sys.stderr)
                time.sleep(0.1)
                continue
            for frame in frames:
                try:
                    message = incoming.push(frame)
                    if message is not None:
                        self.handle_message(message)
                except ValueError as error:
                    print(f"Dropped malformed message: {error}", file=sys.stderr)

    # Writer side: sockets and messages to the client.

    def _send(self, message: bytes, quiet: bool = False) -> None:
        generation = self._generation
        while self._running:
            try:
                write_frames(self.device, message, quiet=quiet)
                return
            except (OSError, TimeoutError):
                # The client is not reading; keep the stream intact unless it restarted.
                if generation != self._generation:
                    return
                time.sleep(0.01)

    def _close(self, upstream: Upstream, error: str | None) -> None:
        upstream.cancel.set()
        if upstream.sock is not None:
            try:
                upstream.sock.close()
            except OSError:
                pass
        self._tunnels.pop(upstream.tunnel, None)
        if error is not None:
            self._control.append(encode_message(CLOSE, upstream.tunnel, error.encode()))

    def _write_loop(self) -> None:
        while self._running:
            with self._lock:
                for upstream in [item for item in self._tunnels.values() if item.closing]:
                    self._close(upstream, None)
                control = list(self._control)
                self._control.clear()
                readers = {item.sock: item for item in self._tunnels.values()
                           if item.open and item.credit > 0}
                writers = {item.sock: item for item in self._tunnels.values()
                           if item.open and item.pending}
            for message in control:
                self._send(message, quiet=message[0] == CREDIT)
            if control:
                continue
            try:
                readable, writable, _ = select.select(
                    [self._wake_r, *readers], list(writers), [], 1.0)
            except (OSError, ValueError):
                continue  # a socket closed under us; rebuild the lists
            if self._wake_r in readable:
                try:
                    while self._wake_r.recv(4096):
                        pass
                except OSError:
                    pass  # drained, or closed during shutdown
            with self._lock:
                for sock in writable:
                    upstream = writers[sock]
                    if upstream.closing:
                        continue
                    try:
                        sent = sock.send(upstream.pending)
                    except BlockingIOError:
                        continue
                    except OSError as error:
                        self._close(upstream, str(error))
                        continue
                    del upstream.pending[:sent]
                    upstream.flushed += sent
                    if upstream.flushed >= CREDIT_STEP or not upstream.pending:
                        self._control.append(encode_message(
                            CREDIT, upstream.tunnel, encode_credit(upstream.flushed)))
                        upstream.flushed = 0
                ready = [readers[sock] for sock in readable if sock in readers]
                ready = [item for item in ready if not item.closing]
                if not ready:
                    continue
                now = time.monotonic()
                upstream = min(ready, key=lambda item: item.score(now))
                try:
                    data = upstream.sock.recv(min(self._piece, upstream.credit))
                except BlockingIOError:
                    continue
                except OSError as error:
                    self._close(upstream, str(error))
                    continue
                if not data:
                    self._close(upstream, "")
                    continue
                upstream.credit -= len(data)
                upstream.charge(len(data))
                message = encode_message(DATA, upstream.tunnel, data)
            self._send(message)

    def serve_forever(self) -> None:
        if self.device is None:
            self.device = open_bulk("server")
        print("USBnet TCP tunnel server connected", flush=True)
        self._running = True
        reader = threading.Thread(target=self._read_loop, daemon=True)
        reader.start()
        try:
            self._write_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    def close(self) -> None:
        self._running = False
        with self._lock:
            for upstream in list(self._tunnels.values()):
                self._close(upstream, None)
        self._workers.shutdown(wait=False, cancel_futures=True)
        self._wake()
        for sock in (self._wake_r, self._wake_w):
            sock.close()
        if self._owns_device and self.device is not None:
            self.device.close()
            self.device = None

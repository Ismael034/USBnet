from __future__ import annotations

import struct

REPORT_SIZE = 64
PAYLOAD_SIZE = REPORT_SIZE - 4
MAGIC = 0x55
START = 1
END = 2
QUIET = 4

# Tunnel messages. Every message is <kind:u8><tunnel id:u32><payload>.
HELLO = 1    # client started; the server drops all tunnels
OPEN = 2     # payload: port u16 + host
OPENED = 3
DATA = 4
CLOSE = 5    # payload: error text, empty for a normal close
CREDIT = 6   # payload: u32 bytes the peer may send on this tunnel
HEADER = struct.Struct("<BI")
# A DATA piece fills exactly 64 frames, one 4096-byte HID report.
PIECE = 64 * PAYLOAD_SIZE - HEADER.size
WINDOW = 256 * 1024  # bytes either side may send per tunnel before credit
CREDIT_STEP = 64 * 1024


def frame_message(message: bytes, *, quiet: bool = False) -> bytes:
    """Encode one logical message as fixed-size USBnet reports."""
    reports = []
    for offset in range(0, max(1, len(message)), PAYLOAD_SIZE):
        payload = message[offset:offset + PAYLOAD_SIZE]
        flags = (QUIET if quiet else 0)
        if offset == 0:
            flags |= START
        if offset + PAYLOAD_SIZE >= len(message):
            flags |= END
        reports.append(
            bytes((MAGIC, flags, (offset // PAYLOAD_SIZE) & 0xFF, len(payload)))
            + payload.ljust(PAYLOAD_SIZE, b"\0")
        )
    return b"".join(reports)


class FrameReassembler:
    """Validate and reassemble ordered USBnet reports into one message."""

    def __init__(self) -> None:
        self._data = bytearray()
        self._next_sequence = 0
        self._active = False

    def reset(self) -> None:
        self._data.clear()
        self._next_sequence = 0
        self._active = False

    def push(self, report: bytes) -> bytes | None:
        if len(report) == REPORT_SIZE + 1 and report[0] == 0:
            report = report[1:]
        if (len(report) != REPORT_SIZE or report[0] != MAGIC
                or report[3] > PAYLOAD_SIZE):
            self.reset()
            raise ValueError("invalid USB frame")

        flags, sequence, length = report[1:4]
        if flags & ~(START | END | QUIET):
            raise ValueError("invalid frame flags")
        if flags & START:
            if sequence != 0:
                raise ValueError("START frame must have sequence zero")
            self.reset()
            self._active = True
        if not self._active or sequence != self._next_sequence:
            self.reset()
            raise ValueError("missing or out-of-order USB frame")

        self._next_sequence = (self._next_sequence + 1) & 0xFF
        self._data.extend(report[4:4 + length])
        if not flags & END:
            return None

        message = bytes(self._data)
        self.reset()
        return message


def read_frames(device, timeout: int) -> list[bytes]:
    """Read one USB transfer and return its non-padding reports."""
    transfer = bytes(device.read(device.block, timeout))
    return [
        transfer[offset:offset + REPORT_SIZE]
        for offset in range(0, len(transfer) - REPORT_SIZE + 1, REPORT_SIZE)
        if transfer[offset]
    ]


def write_frames(device, message: bytes, *, quiet: bool = False) -> None:
    """Write a logical message, rejecting short host USB transfers."""
    transfer = frame_message(message, quiet=quiet)
    written = device.write(transfer)
    if written != len(transfer):
        raise OSError(f"short USB write: {written}/{len(transfer)} bytes")


def encode_message(kind: int, tunnel: int = 0, payload: bytes = b"") -> bytes:
    return HEADER.pack(kind, tunnel) + payload


def decode_message(raw: bytes) -> tuple[int, int, bytes]:
    if len(raw) < HEADER.size:
        raise ValueError("truncated tunnel message")
    kind, tunnel = HEADER.unpack_from(raw)
    return kind, tunnel, raw[HEADER.size:]


def encode_open(host: str, port: int) -> bytes:
    return port.to_bytes(2, "little") + host.encode("utf-8")


def decode_open(payload: bytes) -> tuple[str, int]:
    if len(payload) < 3:
        raise ValueError("invalid OPEN message")
    return payload[2:].decode("utf-8"), int.from_bytes(payload[:2], "little")


def encode_credit(size: int) -> bytes:
    return size.to_bytes(4, "little")


def decode_credit(payload: bytes) -> int:
    if len(payload) != 4:
        raise ValueError("invalid CREDIT message")
    return int.from_bytes(payload, "little")

"""USBnet tunnel over the vendor feature report of an emulated HID device."""
from __future__ import annotations

import ctypes
import sys
import time

try:
    import hid
except ImportError:  # pragma: no cover
    hid = None

FEATURE_SIZES = (4096, 2048)  # USBNET_TUNNEL_FEATURE_LEN, newest firmware first
WRITE_RETRY = 3.0
REPORT_SIZE = 64
FULL_REPORTS = sys.platform == "win32"
EXCLUDED_PRODUCTS = {"USBnet Server", "USBnet Configuration"}


def feature_reports(descriptor: bytes) -> dict[int, int]:
    """Return {report_id: size in bytes} for the feature reports in a HID descriptor."""
    bits: dict[int, int] = {}
    report_id = report_size = report_count = 0
    stack = []
    index = 0
    while index < len(descriptor):
        prefix = descriptor[index]
        if prefix == 0xFE:  # long item
            if index + 1 >= len(descriptor):
                break
            index += 3 + descriptor[index + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        item_type = (prefix >> 2) & 0x03
        tag = prefix >> 4
        value = int.from_bytes(descriptor[index + 1:index + 1 + size], "little")
        index += 1 + size
        if item_type == 1:  # global
            if tag == 7:
                report_size = value
            elif tag == 8:
                report_id = value
            elif tag == 9:
                report_count = value
            elif tag == 10:
                stack.append((report_id, report_size, report_count))
            elif tag == 11 and stack:
                report_id, report_size, report_count = stack.pop()
        elif item_type == 0 and tag == 11:  # feature
            bits[report_id] = bits.get(report_id, 0) + report_size * report_count
    return {key: (value + 7) // 8 for key, value in bits.items()}


def find_tunnel(descriptor: bytes) -> tuple[int, int] | None:
    """Return (report_id, size) of the tunnel feature report, if any."""
    for report_id, size in sorted(feature_reports(descriptor).items()):
        if size in FEATURE_SIZES:
            return report_id, size
    return None


class FeatureDevice:
    """BulkDevice-compatible reads and writes over the tunnel feature report."""


    def __init__(self, device, report_id: int, size: int = FEATURE_SIZES[-1]) -> None:
        self.device = device
        self.report_id = report_id
        self.block = size
        self._closed = False

    def write(self, data: bytes) -> int:
        for offset in range(0, len(data), self.block):
            chunk = data[offset:offset + self.block]
            # Windows only accepts full-size reports.
            length = self.block if FULL_REPORTS else -(-len(chunk) // REPORT_SIZE) * REPORT_SIZE
            report = bytes((self.report_id,)) + chunk.ljust(length, b"\0")
            deadline = time.monotonic() + WRITE_RETRY
            delay = 0.0002
            while True:
                try:
                    written = self.device.send_feature_report(report)
                except (OSError, ValueError) as error:
                    written, failure = -1, error
                else:
                    failure = None
                if written >= len(report) - 1:
                    break
                # The board refuses a report while its queue is full.
                if time.monotonic() >= deadline:
                    raise OSError(f"HID feature write failed: {failure or f'{written}/{len(report)} bytes'}")
                time.sleep(delay)
                delay = min(delay * 2, 0.005)
        return len(data)

    def read(self, size: int, timeout: int = 500) -> bytes:
        deadline = time.monotonic() + timeout / 1000
        while True:
            try:
                # Drop the report ID byte HIDAPI puts first.
                report = bytes(self.device.get_feature_report(self.report_id, self.block + 1))[1:]
            except (OSError, ValueError):
                report = b""  # the board resends frames from a failed transfer
            if any(report):
                return report[:size]
            if time.monotonic() >= deadline:
                return b""

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.device.close()

    def __enter__(self) -> "FeatureDevice":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def share_devices() -> None:
    """Don't seize devices on macOS, so the emulated mouse or keyboard keeps working."""
    if sys.platform != "darwin":
        return
    try:
        ctypes.CDLL(hid.__file__).hid_darwin_set_open_exclusive(0)
    except (AttributeError, OSError):
        pass


def open_feature() -> FeatureDevice:
    """Open the one connected HID device that has a USBnet tunnel feature report."""
    if hid is None:
        raise ImportError("HIDAPI is not installed; run `uv sync` from the repository root")
    share_devices()
    found = []
    seen = set()
    for info in hid.enumerate():
        if info.get("product_string") in EXCLUDED_PRODUCTS or info["path"] in seen:
            continue
        seen.add(info["path"])
        device = hid.device()
        try:
            device.open_path(info["path"])
            tunnel = find_tunnel(bytes(device.get_report_descriptor()))
        except (OSError, ValueError):
            device.close()
            continue
        if tunnel is None:
            device.close()
        else:
            found.append((device, tunnel))
    if len(found) != 1:
        for device, _ in found:
            device.close()
        raise OSError(f"expected one USBnet HID tunnel device, found {len(found)}")
    device, (report_id, size) = found[0]
    return FeatureDevice(device, report_id, size)

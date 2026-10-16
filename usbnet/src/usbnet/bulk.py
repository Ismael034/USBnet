from typing import Literal

import usb.core
import usb.util

VID = 0x1A86
PID = 0xFE51
INTERFACE = 1
OUT_ENDPOINT = 0x05
IN_ENDPOINT = 0x84
BLOCK = 8192
BURST_OUT = 4096
import os as _os
_PIECE = int(_os.environ.get("PIECE", "0")) or None


class BulkDevice:
    duplex = True  # reads and writes may run on separate threads
    block = BLOCK
    piece = _PIECE if _PIECE else 3835

    def __init__(self, device: usb.core.Device) -> None:
        self.device = device
        self._closed = False
        self._claimed = False
        try:
            try:
                device.get_active_configuration()
            except usb.core.USBError:
                device.set_configuration()
            usb.util.claim_interface(device, INTERFACE)
            self._claimed = True
            # A zero report selects bulk routing without injecting a USBnet frame.
            self.write(bytes(64))
        except BaseException:
            self.close()
            raise

    def write(self, data: bytes) -> int:
        # A burst OUT transfer completes on a short packet or at BURST_OUT.
        # Add an ignored zero frame if the last burst ends on a 512-byte packet.
        payload = data
        if len(data) % 512 == 0 and len(data) % BURST_OUT:
            payload += bytes(64)
        written = self.device.write(OUT_ENDPOINT, payload, timeout=3000)
        if written != len(payload):
            raise OSError(f"short USB bulk write: {written}/{len(payload)} bytes")
        return len(data)

    def read(self, size: int, timeout: int = 500) -> bytes:
        try:
            return bytes(self.device.read(IN_ENDPOINT, size, timeout=timeout))
        except usb.core.USBTimeoutError:
            return b""

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._claimed:
                usb.util.release_interface(self.device, INTERFACE)
        finally:
            usb.util.dispose_resources(self.device)

    def __enter__(self) -> "BulkDevice":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def open_bulk(role: Literal["client", "server"]) -> BulkDevice:
    """Open exactly one relay port by its firmware product string."""
    if role not in ("client", "server"):
        raise ValueError("role must be 'client' or 'server'")
    name = "USBnet Server" if role == "server" else "USBnet Client"
    found = []
    for device in usb.core.find(find_all=True, idVendor=VID, idProduct=PID):
        try:
            if usb.util.get_string(device, device.iProduct) == name:
                found.append(device)
        except usb.core.USBError:
            continue
    if len(found) != 1:
        raise OSError("expected one {} bulk interface, found {}".format(name, len(found)))
    return BulkDevice(found[0])

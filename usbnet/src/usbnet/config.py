from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from dataclasses import dataclass, field

VID, PID = 0x1A86, 0xFE53
PROFILE_SIZE = 675
REPORT_DESC_MAX = 512
STRING_SIZE = 48
MAX_STRING = STRING_SIZE - 1

MAGIC = 0xC7
REPLY = 0x80
CMD_INFO, CMD_READ, CMD_BEGIN, CMD_WRITE, CMD_COMMIT = 1, 2, 3, 4, 5
STATUS = {
    0: "ok",
    1: "bad request",
    2: "bad profile",
    3: "flash error",
}

REPORT_READ_CHUNK = 57
REPORT_WRITE_CHUNK = 58
COMMAND_TIMEOUT = 3.0
COMMIT_TIMEOUT = 10.0

MODE_RELAY, MODE_CUSTOM = 0, 1
MODE_NAMES = {MODE_RELAY: "relay", MODE_CUSTOM: "custom"}

ATTRIBUTE_BUS_POWERED = 0x80
ATTRIBUTE_SELF_POWERED = 0x40
ATTRIBUTE_REMOTE_WAKEUP = 0x20

try:
    import hid
except ImportError:  # pragma: no cover - depends on the environment
    hid = None


PRESETS = {
    "vendor": {
        "input_size": 64,
        "output_size": 64,
        "interface_subclass": 0,
        "interface_protocol": 0,
        "report_desc": bytes.fromhex(
            "06 00 FF 09 01 A1 01 15 00 26 FF 00 75 08 95 40"
            "09 02 81 02 95 40 09 03 91 02 C0"
        ),
    },
    "keyboard": {
        "input_size": 8,
        "output_size": 1,
        "interface_subclass": 1,
        "interface_protocol": 1,
        "report_desc": bytes.fromhex(
            "05 01 09 06 A1 01 05 07 19 E0 29 E7 15 00 25 01 75 01 95 08 81 02"
            "95 01 75 08 81 01 95 05 75 01 05 08 19 01 29 05 91 02 95 01 75 03"
            "91 01 95 06 75 08 15 00 25 65 05 07 19 00 29 65 81 00 C0"
        ),
    },
    "mouse": {
        "input_size": 4,
        "output_size": 1,
        "interface_subclass": 1,
        "interface_protocol": 2,
        "report_desc": bytes.fromhex(
            "05 01 09 02 A1 01 09 01 A1 00 05 09 19 01 29 03 15 00 25 01 95 03"
            "75 01 81 02 95 01 75 05 81 01 05 01 09 30 09 31 09 38 15 81 25 7F"
            "75 08 95 03 81 06 C0 C0"
        ),
    },
}


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def require_hid():
    if hid is None:
        die("HIDAPI is not installed; run `uv sync` from the repository root")
    return hid


def path_text(path) -> str:
    return path.decode(errors="replace") if isinstance(path, (bytes, bytearray)) else str(path)


@dataclass
class Profile:
    """The 675-byte usbnet_profile_t, little-endian on the wire."""

    mode: int = MODE_RELAY
    vid: int = 0x1A86
    pid: int = 0xFE51
    bcd_device: int = 0x0100
    device_class: int = 0
    device_subclass: int = 0
    device_protocol: int = 0
    interface_subclass: int = 0
    interface_protocol: int = 0
    interval: int = 1
    max_power_units: int = 50  # USB attribute units of 2 mA -> 100 mA
    attributes: int = ATTRIBUTE_BUS_POWERED
    input_size: int = 64
    output_size: int = 64
    manufacturer: str = "USBnet"
    product: str = "USBnet HID Device"
    serial: str = ""
    report_desc: bytes = field(default_factory=lambda: bytes(PRESETS["vendor"]["report_desc"]))

    @classmethod
    def defaults(cls) -> "Profile":
        return cls()

    @property
    def max_power_ma(self) -> int:
        return self.max_power_units * 2

    @property
    def report_len(self) -> int:
        return len(self.report_desc)

    def to_bytes(self) -> bytes:
        blob = bytearray(PROFILE_SIZE)
        blob[0] = self.mode & 0xFF
        struct.pack_into("<HHH", blob, 1, self.vid & 0xFFFF, self.pid & 0xFFFF, self.bcd_device & 0xFFFF)
        blob[7] = self.device_class & 0xFF
        blob[8] = self.device_subclass & 0xFF
        blob[9] = self.device_protocol & 0xFF
        blob[10] = self.interface_subclass & 0xFF
        blob[11] = self.interface_protocol & 0xFF
        blob[12] = self.interval & 0xFF
        blob[13] = self.max_power_units & 0xFF
        blob[14] = self.attributes & 0xFF
        blob[15] = self.input_size & 0xFF
        blob[16] = self.output_size & 0xFF
        struct.pack_into("<H", blob, 17, self.report_len & 0xFFFF)
        for offset, text in ((19, self.manufacturer), (67, self.product), (115, self.serial)):
            encoded = text.encode("ascii", "replace")[:MAX_STRING]
            blob[offset:offset + len(encoded)] = encoded
        blob[163:163 + self.report_len] = self.report_desc
        return bytes(blob)

    @classmethod
    def from_bytes(cls, blob: bytes) -> "Profile":
        if len(blob) != PROFILE_SIZE:
            die(f"board returned {len(blob)} bytes; expected {PROFILE_SIZE}")
        (vid, pid, bcd_device) = struct.unpack_from("<HHH", blob, 1)
        report_len = struct.unpack_from("<H", blob, 17)[0]

        def read_string(offset: int) -> str:
            chunk = blob[offset:offset + STRING_SIZE]
            end = chunk.find(0)
            if end < 0:
                end = STRING_SIZE
            return chunk[:end].decode("ascii", "replace")

        return cls(
            mode=blob[0],
            vid=vid,
            pid=pid,
            bcd_device=bcd_device,
            device_class=blob[7],
            device_subclass=blob[8],
            device_protocol=blob[9],
            interface_subclass=blob[10],
            interface_protocol=blob[11],
            interval=blob[12],
            max_power_units=blob[13],
            attributes=blob[14],
            input_size=blob[15],
            output_size=blob[16],
            manufacturer=read_string(19),
            product=read_string(67),
            serial=read_string(115),
            report_desc=bytes(blob[163:163 + report_len]),
        )

    def to_dict(self) -> dict:
        data = self.__dict__.copy()
        data["report_desc"] = self.report_desc.hex(" ")
        data["report_len"] = self.report_len
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        fields = {"report_desc", "report_len", "max_power_ma"}
        kwargs = {key: value for key, value in data.items() if key not in fields}
        descriptor = data.get("report_desc") or ""
        if isinstance(descriptor, str):
            try:
                kwargs["report_desc"] = bytes.fromhex(descriptor)
            except ValueError:
                die("report_desc must be hexadecimal bytes")
        return cls(**kwargs)

    def validate(self) -> list:
        problems = []
        if self.mode not in (MODE_RELAY, MODE_CUSTOM):
            problems.append("mode must be 0 (relay) or 1 (custom)")
        if not 0 < self.vid <= 0xFFFF:
            problems.append("VID must be 1..0xFFFF")
        if not 0 < self.pid <= 0xFFFF:
            problems.append("PID must be 1..0xFFFF")
        if not 0 < self.report_len <= REPORT_DESC_MAX:
            problems.append(f"report descriptor must be 1..{REPORT_DESC_MAX} bytes")
        if not 1 <= self.input_size <= 64:
            problems.append("input size must be 1..64")
        if not 1 <= self.output_size <= 64:
            problems.append("output size must be 1..64")
        if not 1 <= self.interval <= 16:
            problems.append("poll interval must be 1..16")
        if not self.attributes & ATTRIBUTE_BUS_POWERED:
            problems.append("attributes must keep the 0x80 bit set")
        for name in ("manufacturer", "product", "serial"):
            value = getattr(self, name)
            if len(value) > MAX_STRING:
                problems.append(f"{name} must be at most {MAX_STRING} characters")
            if any(ord(char) < 0x20 or ord(char) > 0x7E for char in value):
                problems.append(f"{name} must be printable ASCII")
        return problems


def format_profile(profile: Profile, saved: "bool | None" = None) -> str:
    lines = []
    if saved is not None:
        lines.append(f"stored:          {'yes' if saved else 'no (showing defaults)'}")
    lines.append(f"mode:            {MODE_NAMES.get(profile.mode, profile.mode)} ({profile.mode})")
    lines.append(f"vid:             0x{profile.vid:04X}")
    lines.append(f"pid:             0x{profile.pid:04X}")
    lines.append(f"bcdDevice:       0x{profile.bcd_device:04X}")
    lines.append(f"manufacturer:    {profile.manufacturer!r}")
    lines.append(f"product:         {profile.product!r}")
    lines.append(f"serial:          {profile.serial!r}")
    lines.append(
        "device class:    "
        f"0x{profile.device_class:02X}/0x{profile.device_subclass:02X}/0x{profile.device_protocol:02X}"
    )
    lines.append(
        "interface:       "
        f"subclass {profile.interface_subclass}, protocol {profile.interface_protocol}"
    )
    lines.append(f"poll interval:   {profile.interval}")
    lines.append(f"max power:       {profile.max_power_ma} mA ({profile.max_power_units} units)")
    lines.append(
        "attributes:      "
        f"0x{profile.attributes:02X} "
        f"(self-powered={bool(profile.attributes & ATTRIBUTE_SELF_POWERED)}, "
        f"remote-wakeup={bool(profile.attributes & ATTRIBUTE_REMOTE_WAKEUP)})"
    )
    lines.append(f"input/output:    {profile.input_size} / {profile.output_size} bytes")
    lines.append(f"report desc:     {profile.report_len} bytes")
    lines.append("  " + profile.report_desc.hex(" "))
    return "\n".join(lines)


class Board:
    """HID transport for the fixed configuration device."""

    def __init__(self, device, path: str):
        self.device = device
        self.path = path
        self.sequence = 0

    def close(self) -> None:
        try:
            self.device.close()
        except Exception:
            pass

    def _next_sequence(self) -> int:
        self.sequence = (self.sequence + 1) & 0xFF
        return self.sequence

    def command(self, op: int, offset: int = 0, data: bytes = b"", length: "int | None" = None,
                timeout: float = COMMAND_TIMEOUT) -> bytes:
        if length is None:
            length = len(data)
        sequence = self._next_sequence()
        request = bytearray(64)
        request[0] = MAGIC
        request[1] = op
        request[2] = sequence
        request[3] = length & 0xFF
        request[4] = offset & 0xFF
        request[5] = (offset >> 8) & 0xFF
        request[6:6 + len(data)] = data
        self.device.write(b"\x00" + bytes(request))

        deadline = _monotonic() + timeout
        while True:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no reply to command {op} within {timeout:.1f}s")
            reply = bytes(self.device.read(65, int(remaining * 1000)))
            if len(reply) == 65 and reply[0] == 0x00:
                reply = reply[1:]
            if len(reply) != 64 or reply[0] != MAGIC:
                continue
            if reply[1] != (op | REPLY) or reply[2] != sequence:
                continue
            status = reply[3]
            if status != 0:
                raise RuntimeError(f"board rejected command {op}: {STATUS.get(status, status)}")
            return reply


def _monotonic() -> float:
    import time

    return time.monotonic()


def find_boards() -> list:
    hid_module = require_hid()
    devices = hid_module.enumerate(VID, PID)
    vendor = [d for d in devices if d.get("usage_page", 0xFF00) == 0xFF00]
    return vendor or devices


# When a device exposes several HID interfaces, clone the one that best
# represents it: mouse, then keyboard, then consumer control.
_USAGE_PRIORITY = {
    (0x01, 0x02): 0,  # Generic Desktop / Mouse
    (0x01, 0x06): 1,  # Generic Desktop / Keyboard
    (0x0C, 0x01): 2,  # Consumer / Consumer Control
}


def _usage_priority(device: dict) -> int:
    usage = (device.get("usage_page", 0), device.get("usage", 0))
    if usage in _USAGE_PRIORITY:
        return _USAGE_PRIORITY[usage]
    return 3 if usage[0] == 0x01 else 4


def find_clone_sources() -> list:
    hid_module = require_hid()
    best = {}
    board_products = {PID, 0xFE51, 0xFE52}  # config, relay, and hardware-test firmware
    for device in hid_module.enumerate():
        if device.get("vendor_id") == VID and device.get("product_id") in board_products:
            continue  # the board itself
        vendor_id = device.get("vendor_id") or 0
        product_id = device.get("product_id") or 0
        if not vendor_id or not product_id:
            continue  # virtual/Bluetooth collections have no cloneable USB identity
        key = (
            vendor_id,
            product_id,
            device.get("product_string"),
            device.get("manufacturer_string"),
            device.get("serial_number"),
        )
        priority = _usage_priority(device)
        if key not in best or priority < best[key][0]:
            best[key] = (priority, device)
    sources = [device for _, device in best.values()]
    # Sort so the index shown by `list` stays stable between runs.
    sources.sort(
        key=lambda d: (
            d.get("vendor_id") or 0,
            d.get("product_id") or 0,
            (d.get("product_string") or "").lower(),
            (d.get("manufacturer_string") or "").lower(),
        )
    )
    return sources


def open_board(args) -> Board:
    hid_module = require_hid()
    boards = find_boards()
    if args.device_path:
        boards = [d for d in boards if path_text(d["path"]) == args.device_path]
    if not boards:
        die(
            "USBnet Configuration device (1a86:fe53) not found. "
            "Hold USER1 while resetting the board, then connect USB1."
        )
    if args.device_path:
        chosen = boards[0]
    else:
        if not 0 <= args.index < len(boards):
            die(f"found {len(boards)} board(s); choose --index 0..{len(boards) - 1}")
        chosen = boards[args.index]
    device = hid_module.device()
    try:
        device.open_path(chosen["path"])
    except Exception as error:  # noqa: BLE001 - surface the HIDAPI message
        die(f"could not open {path_text(chosen['path'])}: {error}")
    return Board(device, path_text(chosen["path"]))


def read_profile(board: Board) -> tuple:
    info = board.command(CMD_INFO)
    if info[7] != 1:
        die(f"unsupported board wire format version {info[7]}")
    size = info[8] | (info[9] << 8)
    if size != PROFILE_SIZE:
        die(f"board reports profile size {size}; expected {PROFILE_SIZE}")
    blob = bytearray(PROFILE_SIZE)
    for offset in range(0, PROFILE_SIZE, REPORT_READ_CHUNK):
        length = min(REPORT_READ_CHUNK, PROFILE_SIZE - offset)
        reply = board.command(CMD_READ, offset, length=length)
        if reply[4] != length or reply[5] != (offset & 0xFF) or reply[6] != (offset >> 8):
            die("board returned a mismatched read reply")
        blob[offset:offset + length] = reply[7:7 + length]
    return Profile.from_bytes(bytes(blob)), bool(info[10])


def write_profile(board: Board, profile: Profile) -> None:
    problems = profile.validate()
    if problems:
        die("refusing to write: " + "; ".join(problems))
    blob = profile.to_bytes()
    board.command(CMD_BEGIN)
    for offset in range(0, PROFILE_SIZE, REPORT_WRITE_CHUNK):
        board.command(CMD_WRITE, offset, blob[offset:offset + REPORT_WRITE_CHUNK])
    board.command(CMD_COMMIT, timeout=COMMIT_TIMEOUT)


def apply_preset(profile: Profile, name: str, explicit_mode: bool) -> None:
    if name == "custom":
        return
    preset = PRESETS[name]
    profile.report_desc = bytes(preset["report_desc"])
    profile.input_size = preset["input_size"]
    profile.output_size = preset["output_size"]
    profile.interface_subclass = preset["interface_subclass"]
    profile.interface_protocol = preset["interface_protocol"]
    if name != "vendor" and not explicit_mode:
        profile.mode = MODE_CUSTOM


def load_profile_file(path: str) -> Profile:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as error:
        die(f"could not read {path}: {error}")
    if path.lower().endswith(".json") or raw.lstrip().startswith(b"{"):
        try:
            return Profile.from_dict(json.loads(raw))
        except (ValueError, TypeError) as error:
            die(f"{path} is not a valid profile JSON file: {error}")
    return Profile.from_bytes(raw)


def read_descriptor_file(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as error:
        die(f"could not read {path}: {error}")
    text = raw.decode("ascii", "ignore").strip()
    if text and all(char in "0123456789abcdefABCDEFxX \t\r\n," for char in text):
        compact = text.replace("0x", "").replace("0X", "").replace(",", "")
        compact = "".join(compact.split())
        if compact and len(compact) % 2 == 0:
            try:
                return bytes.fromhex(compact)
            except ValueError:
                pass
    return raw


def linux_report_descriptor(path) -> "bytes | None":
    """Best effort: hidraw exposes the report descriptor through sysfs on Linux."""
    if not path_text(path).startswith("/dev/hidraw"):
        return None
    candidate = f"/sys/class/hidraw/{os.path.basename(path_text(path))}/device/report_descriptor"
    try:
        with open(candidate, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def macos_report_descriptor(device: dict) -> "bytes | None":
    """Read the HID report descriptor from IOKit's IORegistry on macOS.

    hidapi reports the device service ID as `DevSrvsID:<n>`, which matches the
    node's IORegistryEntryID. Fall back to VID/PID/product when it does not.
    """
    import plistlib
    import subprocess

    try:
        raw = subprocess.check_output(
            ["ioreg", "-a", "-r", "-c", "IOHIDDevice", "-l", "-w0"],
            stderr=subprocess.DEVNULL,
        )
        nodes = plistlib.loads(raw)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

    text = path_text(device.get("path"))
    wanted_id = None
    if text.startswith("DevSrvsID:"):
        try:
            wanted_id = int(text[len("DevSrvsID:"):])
        except ValueError:
            wanted_id = None

    def descriptor_of(node):
        value = node.get("ReportDescriptor")
        return bytes(value) if isinstance(value, (bytes, bytearray)) and value else None

    for node in nodes:
        if wanted_id is not None and node.get("IORegistryEntryID") == wanted_id:
            found = descriptor_of(node)
            if found:
                return found

    for node in nodes:
        if (
            node.get("VendorID") == device.get("vendor_id")
            and node.get("ProductID") == device.get("product_id")
            and node.get("Product") == device.get("product_string")
            and (node.get("SerialNumber") or None) == (device.get("serial_number") or None)
        ):
            found = descriptor_of(node)
            if found:
                return found
    return None


def system_report_descriptor(device: dict) -> "bytes | None":
    """Fetch a connected device's report descriptor when the OS exposes it."""
    descriptor = linux_report_descriptor(device.get("path"))
    if descriptor:
        return descriptor
    if sys.platform == "darwin":
        return macos_report_descriptor(device)
    return None


def hid_report_sizes(descriptor: bytes) -> tuple:
    """Return (input_size, output_size) in bytes for a HID report descriptor.

    Sums the Input/Output fields per report ID, rounds up to whole bytes, and
    adds the report ID byte when the descriptor uses report IDs. A zero means
    the descriptor declares no report of that kind.
    """
    input_bits = {}
    output_bits = {}
    report_id = 0
    report_size = 0
    report_count = 0
    uses_report_id = False
    index = 0
    total = len(descriptor)
    while index < total:
        prefix = descriptor[index]
        if prefix == 0xFE:  # long item: 0xFE, size, tag, data...
            if index + 2 >= total:
                break
            index += 3 + descriptor[index + 1]
            continue
        size = prefix & 0x03
        if size == 3:
            size = 4
        tag = (prefix >> 4) & 0x0F
        item_type = (prefix >> 2) & 0x03
        value = 0
        for offset in range(size):
            if index + 1 + offset < total:
                value |= descriptor[index + 1 + offset] << (8 * offset)
        index += 1 + size
        if item_type == 1:  # Global
            if tag == 7:  # Report Size
                report_size = value
            elif tag == 8:  # Report ID
                report_id = value
                uses_report_id = True
            elif tag == 9:  # Report Count
                report_count = value
        elif item_type == 0:  # Main
            if tag == 8:  # Input
                input_bits[report_id] = input_bits.get(report_id, 0) + report_size * report_count
            elif tag == 9:  # Output
                output_bits[report_id] = output_bits.get(report_id, 0) + report_size * report_count

    def largest(bits):
        if not bits:
            return 0
        return (1 if uses_report_id else 0) + max((count + 7) // 8 for count in bits.values())

    return largest(input_bits), largest(output_bits)


def apply_options(profile: Profile, args) -> None:
    if args.mode is not None:
        profile.mode = MODE_RELAY if args.mode == "relay" else MODE_CUSTOM
    if args.vid is not None:
        profile.vid = args.vid
    if args.pid is not None:
        profile.pid = args.pid
    if args.bcd is not None:
        profile.bcd_device = args.bcd
    if args.manufacturer is not None:
        profile.manufacturer = args.manufacturer
    if args.product is not None:
        profile.product = args.product
    if args.serial is not None:
        profile.serial = args.serial
    if args.device_class is not None:
        profile.device_class = args.device_class
    if args.device_subclass is not None:
        profile.device_subclass = args.device_subclass
    if args.device_protocol is not None:
        profile.device_protocol = args.device_protocol
    if args.interface_subclass is not None:
        profile.interface_subclass = args.interface_subclass
    if args.interface_protocol is not None:
        profile.interface_protocol = args.interface_protocol
    if args.interval is not None:
        profile.interval = args.interval
    if args.input_size is not None:
        profile.input_size = args.input_size
    if args.output_size is not None:
        profile.output_size = args.output_size
    if args.power is not None:
        profile.max_power_units = (args.power + 1) // 2
    if args.self_powered is not None:
        profile.attributes = (
            profile.attributes | ATTRIBUTE_SELF_POWERED
            if args.self_powered
            else profile.attributes & ~ATTRIBUTE_SELF_POWERED
        )
    if args.remote_wakeup is not None:
        profile.attributes = (
            profile.attributes | ATTRIBUTE_REMOTE_WAKEUP
            if args.remote_wakeup
            else profile.attributes & ~ATTRIBUTE_REMOTE_WAKEUP
        )
    if args.report_file is not None:
        profile.report_desc = read_descriptor_file(args.report_file)
    if args.report_hex is not None:
        compact = args.report_hex.replace("0x", "").replace("0X", "").replace(",", "")
        compact = "".join(compact.split())
        if not compact or len(compact) % 2 or any(c not in "0123456789abcdefABCDEF" for c in compact):
            die("--report-hex needs complete hexadecimal bytes")
        profile.report_desc = bytes.fromhex(compact)
    if profile.report_len > REPORT_DESC_MAX:
        die(f"report descriptor is {profile.report_len} bytes; the maximum is {REPORT_DESC_MAX}")


def confirm(profile: Profile, args) -> bool:
    if args.dry_run or args.yes or not sys.stdin.isatty():
        return True
    print(format_profile(profile))
    return input("Write this profile to the board and reset it? [y/N] ").strip().lower() in ("y", "yes")


def command_list(args) -> int:
    boards = find_boards()
    print("USBnet configuration device:")
    if not boards:
        print("  none - hold USER1 while resetting the board, then connect USB1")
    for index, device in enumerate(boards):
        print(
            f"  [{index}] {device.get('vendor_id', 0):04x}:{device.get('product_id', 0):04x}  "
            f"{device.get('product_string') or 'USBnet Configuration'}  ({path_text(device['path'])})"
        )

    print()
    print("HID devices available to clone (use the index with `clone`):")
    sources = find_clone_sources()
    if not sources:
        print("  none found")
    for index, device in enumerate(sources):
        name = device.get("product_string") or device.get("manufacturer_string") or "(unnamed)"
        usage = f"{device.get('usage_page', 0):04x}:{device.get('usage', 0):04x}"
        serial = device.get("serial_number")
        extra = f"  serial={serial}" if serial else ""
        print(
            f"  [{index}] {device.get('vendor_id', 0):04x}:{device.get('product_id', 0):04x}  "
            f"{name}  (usage {usage}, iface {device.get('interface_number', -1)}){extra}"
        )
    return 0


def command_get(args) -> int:
    board = open_board(args)
    try:
        profile, saved = read_profile(board)
    finally:
        board.close()
    if args.json:
        print(json.dumps(profile.to_dict(), indent=2))
    else:
        print(format_profile(profile, saved))
    if args.out:
        if args.out.lower().endswith(".json"):
            with open(args.out, "w", encoding="utf-8") as handle:
                json.dump(profile.to_dict(), handle, indent=2)
                handle.write("\n")
        else:
            with open(args.out, "wb") as handle:
                handle.write(profile.to_bytes())
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


def command_set(args) -> int:
    board = open_board(args) if not args.no_board else None
    try:
        if args.from_file:
            profile = load_profile_file(args.from_file)
        elif args.defaults:
            profile = Profile.defaults()
        elif board is not None:
            profile, _ = read_profile(board)
        else:
            profile = Profile.defaults()
        if args.preset:
            apply_preset(profile, args.preset, explicit_mode=args.mode is not None)
        apply_options(profile, args)
        problems = profile.validate()
        if problems:
            die("invalid profile: " + "; ".join(problems))
        if args.dry_run:
            print(format_profile(profile))
            return 0
        if board is None:
            die("the board is required to save; connect it in configuration mode or use --dry-run")
        if not confirm(profile, args):
            print("aborted", file=sys.stderr)
            return 1
        write_profile(board, profile)
    finally:
        if board is not None:
            board.close()
    print("saved to internal flash; release USER1 and reset to apply", file=sys.stderr)
    print(format_profile(profile))
    return 0


def command_clone(args) -> int:
    sources = find_clone_sources()
    if not sources:
        die("no connected HID devices found to clone")
    if not 0 <= args.source < len(sources):
        die(f"source index must be 0..{len(sources) - 1} (see `list`)")
    source = sources[args.source]

    board = None if args.no_board else open_board(args)
    try:
        if args.from_file:
            profile = load_profile_file(args.from_file)
        elif board is not None:
            profile, _ = read_profile(board)
        else:
            profile = Profile.defaults()

        profile.vid = source.get("vendor_id") or profile.vid
        profile.pid = source.get("product_id") or profile.pid
        if source.get("manufacturer_string"):
            profile.manufacturer = source["manufacturer_string"]
        if source.get("product_string"):
            profile.product = source["product_string"]
        profile.serial = source.get("serial_number") or ""
        release = source.get("release_number")
        if release:
            profile.bcd_device = release & 0xFFFF
        if args.mode is None:
            profile.mode = MODE_CUSTOM

        descriptor = None
        if args.preset is None and args.report_hex is None and args.report_file is None:
            descriptor = system_report_descriptor(source)
            if descriptor:
                profile.report_desc = descriptor
                input_size, output_size = hid_report_sizes(descriptor)
                if input_size:
                    profile.input_size = min(input_size, 64)
                if output_size:
                    profile.output_size = min(output_size, 64)
                print(
                    f"report descriptor: {len(descriptor)} bytes, "
                    f"input {input_size or profile.input_size} B / output {output_size or profile.output_size} B",
                    file=sys.stderr,
                )
        if args.preset:
            apply_preset(profile, args.preset, explicit_mode=args.mode is not None)
        apply_options(profile, args)

        problems = profile.validate()
        if problems:
            die("invalid profile: " + "; ".join(problems))

        print(
            f"cloned {profile.vid:04x}:{profile.pid:04x} "
            f"{profile.manufacturer!r}/{profile.product!r}",
            file=sys.stderr,
        )
        if args.preset is None and descriptor is None and args.report_hex is None and args.report_file is None:
            print(
                "note: report descriptor was not readable on this OS; keeping the current descriptor",
                file=sys.stderr,
            )
        if args.dry_run:
            print(format_profile(profile))
            return 0
        if board is None:
            die("the board is required to save; connect it in configuration mode or use --dry-run")
        if not confirm(profile, args):
            print("aborted", file=sys.stderr)
            return 1
        write_profile(board, profile)
    finally:
        if board is not None:
            board.close()
    print("saved to internal flash; release USER1 and reset to apply", file=sys.stderr)
    print(format_profile(profile))
    return 0


def hex_int(text: str) -> int:
    try:
        value = int(text, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not hexadecimal")
    if not 0 <= value <= 0xFFFF:
        raise argparse.ArgumentTypeError("value must fit in 16 bits")
    return value


def u8(text: str) -> int:
    try:
        value = int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer")
    if not 0 <= value <= 0xFF:
        raise argparse.ArgumentTypeError("value must be 0..255")
    return value


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--device-path", help="target a specific HID path from `list`")
    common.add_argument("--index", type=int, default=0, help="board index when several are connected")

    parser = argparse.ArgumentParser(
        description="Configure the USBnet board profile over its vendor HID interface.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "list",
        parents=[common],
        help="show the board and the HID devices available to clone",
    )

    get_parser = subparsers.add_parser("get", parents=[common], help="read the stored profile")
    get_parser.add_argument("--json", action="store_true", help="print the profile as JSON")
    get_parser.add_argument("--out", metavar="FILE", help="write the profile to FILE (.json or .bin)")

    clone_parser = subparsers.add_parser(
        "clone", parents=[common], help="copy a connected device's identity into the profile"
    )
    clone_parser.add_argument("source", type=int, help="device index from `list`")
    _add_profile_options(clone_parser)
    clone_parser.add_argument("--from-file", metavar="FILE", help="start from this profile instead of the board's")

    set_parser = subparsers.add_parser("set", parents=[common], help="change fields and write them to flash")
    _add_profile_options(set_parser)
    set_parser.add_argument("--defaults", action="store_true", help="start from factory defaults")
    set_parser.add_argument("--from-file", metavar="FILE", help="start from a saved .json or .bin profile")

    return parser


def _add_profile_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", choices=("relay", "custom"), default=None,
                        help="relay keeps the fixed USBnet USB2 port; custom emulates the profile")
    parser.add_argument("--vid", type=hex_int, default=None, help="vendor id (hex)")
    parser.add_argument("--pid", type=hex_int, default=None, help="product id (hex)")
    parser.add_argument("--bcd", type=hex_int, default=None, help="bcdDevice (hex)")
    parser.add_argument("--manufacturer", default=None)
    parser.add_argument("--product", default=None)
    parser.add_argument("--serial", default=None)
    parser.add_argument("--device-class", type=u8, default=None)
    parser.add_argument("--device-subclass", type=u8, default=None)
    parser.add_argument("--device-protocol", type=u8, default=None)
    parser.add_argument("--interface-subclass", type=u8, default=None)
    parser.add_argument("--interface-protocol", type=u8, default=None)
    parser.add_argument("--interval", type=int, default=None, help="interrupt poll interval, 1..16")
    parser.add_argument("--power", type=int, default=None, help="max power in mA, 0..500")
    parser.add_argument("--self-powered", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--remote-wakeup", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--input-size", type=int, default=None, help="input report bytes, 1..64")
    parser.add_argument("--output-size", type=int, default=None, help="output report bytes, 1..64")
    parser.add_argument("--preset", choices=tuple(PRESETS) + ("custom",), default=None,
                        help="HID report preset: vendor, keyboard, or mouse")
    parser.add_argument("--report-hex", default=None, help="HID report descriptor as hex bytes")
    parser.add_argument("--report-file", default=None, help="HID report descriptor from a file")
    parser.add_argument("--dry-run", action="store_true", help="print the result without writing")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--no-board", action="store_true", help=argparse.SUPPRESS)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "list": command_list,
        "get": command_get,
        "set": command_set,
        "clone": command_clone,
    }
    try:
        return handlers[args.command](args)
    except TimeoutError as error:
        die(str(error))
    except RuntimeError as error:
        die(str(error))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

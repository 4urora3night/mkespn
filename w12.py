"""
mkespn / YJX-CHIP USB MOUSE programmer (VID a8a4, PID 2255 — W12 and similar).

CLI tool to configure the 6 side buttons (A..F) of W12-class mice without
launching the Windows configurator under Wine.

State is stored locally in ~/.config/mkespn/state.json — modify via CLI,
then `apply` writes the full config burst to the device. The keymap section
of the protocol is write-only, so we maintain authoritative state ourselves
rather than reading from the device.

Decoded:
- USB vendor channel: hidraw on interface 2, ep 0x03 OUT / 0x83 IN, 64-byte
  interrupt frames. Frame format: `55 cmd sub <chk=0> length offset_lo offset_hi page <data...>`
- Keymap section (cmd 0d 00): partition table at 0x00..0x37 listing the start
  offset of each side button's macro slot, then per-button macro slots.
- Macro mode section (cmd 09 a5): per-button mode records `70 IDX REPEAT MODE`.
- Macro events: 4-byte records (op | payload | delay-or-flags). LAST event
  has bit 0x80; for moves the LAST event is 2 bytes (no delay/flags slot).
- Modes: 0x02=loop-while-held (PTT), 0x03=single-fire,
  0x00 with count=1=until-any-click, 0x00 with count>=2=loop N times.
- The "checksum" byte at offset 3 of each frame is NOT validated by firmware.

Not decoded yet:
- Right-click / middle-click / DPI button assignments. They are written by
  the wine app in additional fields we haven't isolated. Our `apply` writes
  default ("loop while held") modes for them as a no-op-equivalent and does
  not change their letter assignments.
"""

import json
import os
import select
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

# ---- Constants -----------------------------------------------------------

VID = 0xA8A4
PID = 0x2255
HIDRAW_INTERFACE = 2
FRAME_SIZE = 64
RESP_TIMEOUT_S = 0.3

STATE_DIR = os.path.expanduser("~/.config/mkespn")
STATE_FILE = os.path.join(STATE_DIR, "w12.json")


# ---- HID keys ------------------------------------------------------------

HID_KEYS: Dict[str, int] = {
    **{c: 0x04 + i for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")},
    **{c: 0x1E + i for i, c in enumerate("1234567890")},
    "enter": 0x28, "escape": 0x29, "backspace": 0x2A,
    "tab": 0x2B, "space": 0x2C,
    **{f"f{n}": 0x39 + n for n in range(1, 13)},
    **{f"f{n}": 0x68 + (n - 13) for n in range(13, 25)},
}
HID_KEYS_REVERSE = {v: k for k, v in HID_KEYS.items()}


def hid_keycode(k):
    if isinstance(k, int):
        return k
    if k.lower() not in HID_KEYS:
        raise ValueError(f"unknown key: {k}")
    return HID_KEYS[k.lower()]


def hid_keyname(code: int) -> str:
    return HID_KEYS_REVERSE.get(code, f"0x{code:02x}")


MBTN_LEFT, MBTN_RIGHT, MBTN_MIDDLE = 0x01, 0x02, 0x04


# ---- Macro modes ---------------------------------------------------------

MODE_LOOP_HELD     = 0x02
MODE_SINGLE_FIRE   = 0x03
MODE_LOOP_COUNT    = 0x00

MODE_NAMES = {
    "ptt":         (MODE_LOOP_HELD, 1),
    "single":      (MODE_SINGLE_FIRE, 1),
    "until-click": (MODE_LOOP_COUNT, 1),
}


def parse_mode(s: str) -> tuple:
    if s in MODE_NAMES:
        return MODE_NAMES[s]
    if s.startswith("loop:"):
        return MODE_LOOP_COUNT, int(s.split(":", 1)[1])
    raise ValueError(f"unknown mode: {s} (try ptt | single | until-click | loop:N)")


def mode_label(mode: int, count: int) -> str:
    if mode == MODE_LOOP_HELD:
        return "ptt (loop while held)"
    if mode == MODE_SINGLE_FIRE:
        return "single-fire"
    if mode == MODE_LOOP_COUNT:
        return "until-click" if count == 1 else f"loop x{count}"
    return f"mode=0x{mode:02x} count={count}"


# ---- Macro events --------------------------------------------------------

@dataclass
class Macro:
    """An ordered sequence of events. JSON-friendly."""
    events: List[list] = field(default_factory=list)

    @classmethod
    def tap_key(cls, key) -> "Macro":
        kc = hid_keycode(key)
        return cls(events=[["press_kbd", kc, 1], ["release_kbd", kc, 8]])


def encode_macro(m: Macro) -> bytes:
    """Encode a Macro to wire bytes (with the 0x0a 0x00 header)."""
    out = bytearray(b"\x0a\x00")
    n = len(m.events)
    for i, ev in enumerate(m.events):
        last = (i == n - 1)
        kind = ev[0]
        if kind == "press_kbd":
            kc, d = ev[1], ev[2]
            op = 0x42 | (0x80 if last else 0)
            out += bytes([op, kc, d & 0xFF, (d >> 8) & 0xFF])
        elif kind == "release_kbd":
            kc, d = ev[1], ev[2]
            if last:
                out += bytes([0x82, kc])
            else:
                out += bytes([0x02, kc, d & 0xFF, (d >> 8) & 0xFF])
        elif kind == "press_btn":
            mask, d = ev[1], ev[2]
            op = 0x43 | (0x80 if last else 0)
            out += bytes([op, mask, d & 0xFF, (d >> 8) & 0xFF])
        elif kind == "release_btn":
            mask, d = ev[1], ev[2]
            if last:
                out += bytes([0x83, mask])
            else:
                out += bytes([0x03, mask, d & 0xFF, (d >> 8) & 0xFF])
        elif kind == "move":
            dx, dy, d = ev[1], ev[2], ev[3]
            packed = (min(15, abs(dx)) << 4) | min(15, abs(dy))
            flags = (0x40 if dx >= 0 else 0) | (0x80 if dy >= 0 else 0)
            if last:
                out += bytes([0xC6, packed])
            else:
                out += bytes([0x46, packed, d & 0xFF, flags])
        else:
            raise ValueError(f"unknown event kind: {kind}")
    return bytes(out)


def macro_human(m: Macro) -> str:
    parts = []
    for ev in m.events:
        kind = ev[0]
        if kind in ("press_kbd", "release_kbd"):
            parts.append(f"{'+' if kind == 'press_kbd' else '-'}{hid_keyname(ev[1])}")
        elif kind in ("press_btn", "release_btn"):
            parts.append(f"{'+' if kind == 'press_btn' else '-'}btn{ev[1]:x}")
        elif kind == "move":
            parts.append(f"move({ev[1]:+d},{ev[2]:+d})")
        else:
            parts.append(repr(ev))
    return " ".join(parts)


# ---- Per-button config ---------------------------------------------------

@dataclass
class ButtonConfig:
    macro: Macro = field(default_factory=Macro)
    mode: int = MODE_LOOP_HELD
    count: int = 1


SIDE_BUTTONS = ["A", "B", "C", "D", "E", "F"]


@dataclass
class State:
    """Authoritative tool-side state for the 6 side buttons."""
    buttons: Dict[str, ButtonConfig] = field(default_factory=dict)

    @classmethod
    def default(cls) -> "State":
        s = cls()
        for i, name in enumerate(SIDE_BUTTONS):
            letter = "abcdef"[i]
            s.buttons[name] = ButtonConfig(
                macro=Macro.tap_key(letter),
                mode=MODE_LOOP_HELD,
                count=1,
            )
        return s

    def to_json(self) -> str:
        out = {"buttons": {}}
        for name, cfg in self.buttons.items():
            out["buttons"][name] = {
                "events": cfg.macro.events,
                "mode": cfg.mode,
                "count": cfg.count,
            }
        return json.dumps(out, indent=2)

    @classmethod
    def from_json(cls, s: str) -> "State":
        d = json.loads(s)
        out = cls()
        for name, b in d["buttons"].items():
            out.buttons[name] = ButtonConfig(
                macro=Macro(events=b["events"]),
                mode=b["mode"],
                count=b["count"],
            )
        return out


def load_state() -> State:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return State.from_json(f.read())
    return State.default()


def save_state(state: State):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        f.write(state.to_json())


# ---- Wire buffer assembly -----------------------------------------------

def build_keymap_buffer(state: State) -> bytes:
    """Partition table + reserved + per-button macro slots.

    Uses 0x08 0x00 slot header for buttons B..F (matching wine's output)
    and 0x0a 0x00 only for A. Empirically wine produces this asymmetry
    and the firmware seems to require it for buttons B..F to be active.
    """
    slot_bytes = []
    for i, name in enumerate(SIDE_BUTTONS):
        m = state.buttons[name].macro
        # Encode without the default Macro header so we can pick the right one
        out = bytearray()
        n_ev = len(m.events)
        for j, ev in enumerate(m.events):
            last = (j == n_ev - 1)
            kind = ev[0]
            if kind == "press_kbd":
                kc, d = ev[1], ev[2]
                op = 0x42 | (0x80 if last else 0)
                out += bytes([op, kc, d & 0xFF, (d >> 8) & 0xFF])
            elif kind == "release_kbd":
                kc, d = ev[1], ev[2]
                if last:
                    out += bytes([0x82, kc])
                else:
                    out += bytes([0x02, kc, d & 0xFF, (d >> 8) & 0xFF])
            elif kind == "press_btn":
                mask, d = ev[1], ev[2]
                op = 0x43 | (0x80 if last else 0)
                out += bytes([op, mask, d & 0xFF, (d >> 8) & 0xFF])
            elif kind == "release_btn":
                mask, d = ev[1], ev[2]
                if last:
                    out += bytes([0x83, mask])
                else:
                    out += bytes([0x03, mask, d & 0xFF, (d >> 8) & 0xFF])
            elif kind == "move":
                dx, dy, d = ev[1], ev[2], ev[3]
                packed = (min(15, abs(dx)) << 4) | min(15, abs(dy))
                flags = (0x40 if dx >= 0 else 0) | (0x80 if dy >= 0 else 0)
                if last:
                    out += bytes([0xC6, packed])
                else:
                    out += bytes([0x46, packed, d & 0xFF, flags])
            else:
                raise ValueError(f"unknown event kind: {kind}")
        # Per-button slot header: 0a 00 for A, 08 00 for B..F (mirrors wine)
        header = b"\x0a\x00" if i == 0 else b"\x08\x00"
        slot_bytes.append(header + bytes(out))

    parts = bytearray()
    cursor = 0x40
    for s in slot_bytes:
        parts.extend(cursor.to_bytes(2, "little"))
        cursor += len(s)
    while len(parts) < 0x38:
        parts.append(0)
    parts += b"\x00" * 8
    for s in slot_bytes:
        parts += s
    return bytes(parts)


# Mode-buffer page 0 is templated from a captured wine Apply for the default
# all-side-buttons config; we substitute the per-button mode/count into the
# `70 IDX cnt mode` records. Other records (20, 21, etc.) — which encode
# main-button defaults and RGB info — are preserved verbatim.
# Mode-page-0 data payload — 48 bytes, captured verbatim from a working
# wine Apply for the default ABCDEF state. We patch in per-button mode/count
# at the matching `70 IDX` records. Trailing zero padding is handled by
# _frame() (the frame buffer is pre-zeroed).
MODE_PAGE0_TEMPLATE = bytes.fromhex(
    "20 01 00 00 20 02 00 00 20 04 00 00"
    "70 02 01 02 70 01 01 02 21 55 00 00"
    "21 38 01 00 21 38 ff 00"
    "70 05 01 02 70 00 01 02 70 04 01 02 70 03 01 02"
)
assert len(MODE_PAGE0_TEMPLATE) == 48
# Mapping: byte-position in MODE_PAGE0_TEMPLATE pointing to a `70 IDX` →
# which side-button (A..F) it controls. Derived from observed user changes:
#   IDX 2 ↔ B (the user's "single-fire B")
#   IDX 1 ↔ C
#   IDX 0 ↔ A
#   IDX 4 ↔ E
#   IDX 5 ↔ F
#   IDX 3 ↔ D
SIDE_IDX_TO_NAME = {0: "A", 1: "C", 2: "B", 3: "D", 4: "E", 5: "F"}


def build_mode_page0(state: State) -> bytes:
    out = bytearray(MODE_PAGE0_TEMPLATE)
    i = 0
    while i + 4 <= len(out):
        if out[i] == 0x70:
            wire_idx = out[i + 1]
            name = SIDE_IDX_TO_NAME.get(wire_idx)
            if name in state.buttons:
                cfg = state.buttons[name]
                out[i + 2] = cfg.count & 0xFF
                out[i + 3] = cfg.mode & 0xFF
        i += 4
    return bytes(out)


# Mode-buffer pages 1..4 — verbatim 64-byte frames. The page byte (offset 7)
# differs between them so we keep them as separate full frames.
VERBATIM_MODE_PAGES_1_TO_4 = [
    bytes.fromhex(
        f"55 09 a5 00 30 00 00 0{p} 20 01 00 00 20 02 00 00 20 04 00 00 20 08 00 00 20 10 00 00 21 55 00 00"
        "21 38 01 00 21 38 ff 00 10 08 08 00 21 61 00 00 10 08 07 00 30 e2 00 00 00 00 00 00 00 00 00 00"
    )
    for p in (1, 2, 3, 4)
]

# DPI/RGB tail frames — VERBATIM 64-byte frames captured from a working
# wine Apply. Sent as-is; the firmware seems strict about the length byte
# at offset 4, so we don't recompute it from the data length.
VERBATIM_TAIL_FRAMES = [
    bytes.fromhex(
        "55 0f ae 00 2e 00 00 00 00 00 02 06 03 90 01 20 03 40 06 80 0c 88 13 10 27 ff 00 00 00 ff 00 00"
        "00 ff 80 00 80 ff ff 00 00 ff ff 00 00 00 00 00 00 01 30 08 14 01 00 00 00 00 00 00 00 00 00 00"
    ),
    bytes.fromhex(
        "55 22 ae 00 03 00 00 00 00 00 04 80 0c 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
        "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    ),
    bytes.fromhex(
        "55 23 ae 00 03 00 00 00 00 00 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
        "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    ),
]


# ---- Frame I/O ----------------------------------------------------------

def _frame(cmd: int, sub: int, length: int, offset: int, data: bytes,
           page: int = 0) -> bytearray:
    f = bytearray(FRAME_SIZE)
    f[0] = 0x55
    f[1] = cmd
    f[2] = sub
    f[3] = 0
    f[4] = length
    f[5] = offset & 0xFF
    f[6] = (offset >> 8) & 0xFF
    f[7] = page
    body = data[:FRAME_SIZE - 8]
    f[8:8 + len(body)] = body
    return f


def find_vendor_hidraw(vid=VID, pid=PID, interface=HIDRAW_INTERFACE) -> str:
    sysclass = "/sys/class/hidraw"
    for h in sorted(os.listdir(sysclass)):
        try:
            link = os.path.realpath(os.path.join(sysclass, h, "device"))
        except OSError:
            continue
        if f"{vid:04X}:{pid:04X}" not in link.upper():
            continue
        if f":1.{interface}/" in link:
            return f"/dev/{h}"
    raise FileNotFoundError(f"no hidraw for {vid:04X}:{pid:04X} iface {interface}")


class W12:
    def __init__(self, hidraw_path: Optional[str] = None):
        self.path = hidraw_path or find_vendor_hidraw()
        self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self): return self

    def __exit__(self, *exc): self.close()

    def _drain(self):
        while True:
            r, _, _ = select.select([self.fd], [], [], 0)
            if not r: return
            try: os.read(self.fd, FRAME_SIZE)
            except BlockingIOError: return

    def _exchange(self, frame: bytearray) -> bytes:
        self._drain()
        os.write(self.fd, bytes(frame))
        deadline = time.monotonic() + RESP_TIMEOUT_S
        while time.monotonic() < deadline:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try: resp = os.read(self.fd, FRAME_SIZE)
                except BlockingIOError: continue
                if resp and resp[0] == 0xAA:
                    return resp
        raise RuntimeError("no response from device")

    def query(self, cmd, sub, page=0, length=0, offset=0) -> bytes:
        return self._exchange(_frame(cmd, sub, length, offset, b"", page=page))

    def write(self, cmd, sub, data: bytes, offset=0, page=0) -> bytes:
        return self._exchange(_frame(cmd, sub, len(data), offset, data, page=page))

    # Read commands proven to be safe (length>0 + zero body returns data)
    def read_device_info(self) -> str:
        resp = self.query(0x03, 0x00)
        return "".join(chr(b) if 32 <= b < 127 else "." for b in resp[8:56])

    def read_modes_page(self, page: int) -> bytes:
        resp = self.query(0x08, 0xA5, page=page, length=0x30)
        n = resp[4]
        return resp[8:8 + n]

    # Apply (write the full config burst, mirroring the wine app)
    def apply(self, state: State):
        keymap = build_keymap_buffer(state)
        # 1) keymap (chunked at 56 bytes)
        for off in range(0, len(keymap), 56):
            self.write(0x0D, 0x00, keymap[off:off + 56], offset=off)
        # 2) marker
        self.write(0x10, 0xA5, b"")
        # 3) mode page 0 (per-button modes)
        self.write(0x09, 0xA5, build_mode_page0(state), page=0)
        # 4) mode pages 1..4 — verbatim 64-byte frames
        for frame in VERBATIM_MODE_PAGES_1_TO_4:
            self._exchange(bytearray(frame))
        # 5) DPI/RGB tail — verbatim 64-byte frames
        for frame in VERBATIM_TAIL_FRAMES:
            self._exchange(bytearray(frame))


# ---- CLI ---------------------------------------------------------------

USAGE = """\
mkespn W12 / YJX-CHIP USB MOUSE programmer

Usage:
  w12.py info                      — read device info from the mouse
  w12.py show                      — print the local saved state
  w12.py reset                     — restore default state (a/b/c/d/e/f, all PTT)
  w12.py set <BTN> <KEY> [MODE]    — set side button A..F to a single key
                                     MODE = ptt | single | until-click | loop:N
                                            (default: ptt)
  w12.py apply                     — write current state to the mouse
  w12.py read-modes                — debug: read current modes pages from device

Examples:
  w12.py set A f13 ptt              # PTT button on F13 — perfect for Android
  w12.py set B 1 single             # B = '1', single-fire
  w12.py set C 2 loop:5             # C = '2', repeat 5 times
  w12.py apply                      # push the changes to the device
"""


def cmd_info(_args):
    with W12() as dev:
        print(dev.read_device_info())


def cmd_show(_args):
    state = load_state()
    print(f"state file: {STATE_FILE}")
    for name in SIDE_BUTTONS:
        cfg = state.buttons[name]
        print(f"  {name}: {macro_human(cfg.macro):<30} mode={mode_label(cfg.mode, cfg.count)}")


def cmd_reset(_args):
    save_state(State.default())
    print(f"reset state to defaults at {STATE_FILE}")


def cmd_set(args):
    if len(args) < 2:
        print("set requires <BTN> <KEY> [MODE]", file=sys.stderr); sys.exit(2)
    btn = args[0].upper()
    key = args[1]
    mode_arg = args[2] if len(args) > 2 else "ptt"
    if btn not in SIDE_BUTTONS:
        print(f"unknown button: {btn} (use A..F)", file=sys.stderr); sys.exit(2)
    mode, count = parse_mode(mode_arg)

    state = load_state()
    state.buttons[btn] = ButtonConfig(
        macro=Macro.tap_key(key), mode=mode, count=count,
    )
    save_state(state)
    print(f"saved {btn} → {key}, {mode_label(mode, count)}")
    print("(run 'w12.py apply' to push to the device)")


def cmd_apply(_args):
    state = load_state()
    with W12() as dev:
        dev.apply(state)
    print("applied to device")


def cmd_read_modes(_args):
    with W12() as dev:
        for pg in range(5):
            data = dev.read_modes_page(pg)
            print(f"page {pg} ({len(data)} bytes): {data.hex(' ')}")


COMMANDS = {
    "info":       cmd_info,
    "show":       cmd_show,
    "reset":      cmd_reset,
    "set":        cmd_set,
    "apply":      cmd_apply,
    "read-modes": cmd_read_modes,
}


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    cmd = argv[0]
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    COMMANDS[cmd](argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main())

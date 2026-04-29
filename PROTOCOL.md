# MKESPN W12 / YJX-CHIP USB MOUSE — vendor protocol notes

Reverse-engineered from USB captures of the vendor's Windows configurator
("DeviceDriver.exe", part of `SXS-W12M-V1.2.5.exe`) running under
wine-staging 11.7 on Ubuntu 24.04. The mouse stores all macros in onboard
flash; once written, the configuration persists across host OS changes.

## USB device

```
Bus xxx Device xxx: ID a8a4:2255 YJX-CHIP USB MOUSE
```

Three HID interfaces:

| iface | bInterfaceSubClass | endpoints                  | role                                |
|-------|-------------------|----------------------------|-------------------------------------|
| 0     | 1 (Boot)          | 0x81 IN (8 B)              | standard mouse pointer              |
| 1     | 1 (Boot)          | 0x82 IN, 0x02 OUT (8 B)    | boot keyboard (macro keystrokes)    |
| 2     | 0                 | 0x83 IN, 0x03 OUT (64 B)   | **vendor configuration channel**    |

Configuration goes via interface 2's 64-byte interrupt OUT/IN endpoints.
On Linux these surface as `/dev/hidraw{N,N+1,N+2}` — find the one whose
sysfs path includes `:1.2/`.

The device identifies itself when queried with command `0x03 0x00`:

```
"YJX.co.mouse" + version 2.3 + ID "213" + firmware build dates
```

## Frame format (vendor channel)

All communication on endpoints 0x03/0x83 uses fixed 64-byte frames.

```
offset  byte    meaning
0       0x55    host→device start byte (responses use 0xaa)
1       cmd     command code
2       sub     subcommand code (often 0x00 or 0xa5 / 0xae)
3       chk     "checksum" — NOT actually validated by firmware (see below)
4       len     bytes of payload data following the header
5–6     off     LE 16-bit write offset within the target buffer
7       page    page number when the buffer is paginated
8…63    data    payload (length given by byte 4; rest is padding)
```

The "checksum" byte at offset 3 is NOT validated by firmware. The wine app
fills it with deterministic values for its own bookkeeping, but the device
echoes any value back unchanged and applies the write either way. Set it
to `0`.

Each host-→-device frame is acknowledged by the device with a frame whose
byte 0 is `0xaa` and whose `cmd`/`sub`/`page` echo the request; for write
commands the response also echoes the written data, for read commands it
contains the device's current data.

## Command catalogue

| Cmd  | Sub  | Direction        | Purpose                                                        |
|------|------|------------------|----------------------------------------------------------------|
| 0x03 | 0x00 | host→dev (read)  | Device identification (model name, version, build dates)       |
| 0x08 | 0xa5 | host→dev (read)  | Reads macro-mode buffer page N                                 |
| 0x09 | 0xa5 | host→dev (write) | Writes macro-mode buffer page N                                |
| 0x0d | 0x00 | host→dev (write) | Writes keymap section (partition table + side-button slots)    |
| 0x0e | 0xa5 | host→dev (read)  | Reads DPI / polling / RGB section                              |
| 0x0f | 0xae | host→dev (write) | Writes DPI / polling / RGB section                             |
| 0x10 | 0xa5 | host→dev         | Marker frame sent before the modes write (no payload)          |
| 0x22 | 0xae | host→dev (write) | Auxiliary RGB section                                          |
| 0x23 | 0xae | host→dev (write) | Auxiliary RGB section                                          |
| 0x30 | 0xa5 | host→dev (read)  | Some kind of session/state read at app startup                 |
| 0xfa | 0xae | host→dev (read)  | 8-byte device identifier (serial-like)                         |

Notes:
- Cmd `0x0d 0x00` is **write-only**. Sending it with `length>0` and a
  zero body **clears that range of memory** (do not use as a "read"
  query — there is no separate read for the keymap section).
- Read commands send `length>0` with an all-zero body; the response
  carries the device's current data of that length.
- The marker frame (`0x10 0xa5`) is sent between the keymap write and
  the macro-mode write; purpose unclear but always present.

## Keymap section (cmd `0x0d 0x00`)

The keymap section is a single contiguous buffer:

```
offset    contents
0x00..0x37  partition table (6 × LE 16-bit start offsets, then zero padding)
0x38..0x3F  reserved (always zeros)
0x40..      per-button macro slots, packed back-to-back
```

The partition table holds 6 entries — one per side button A..F — pointing
to the start offset of each button's macro slot. Default layout has each
slot at `0x40 + i*8` (8 bytes per slot for a single-key macro), but slots
are variable-sized so subsequent entries shift when an earlier slot grows.

The wine app writes this section in 56-byte chunks at offsets `0x00`,
`0x38`, `0x70` (and so on if the buffer is larger).

## Per-button macro slot

Each slot starts with a 2-byte **slot header** (purpose unclear; observed
to be `0a 00` for button A and `08 00` for buttons B..F — probably a
default-delay value the firmware reads on macro start). After the header
comes a sequence of **event records**:

```
4-byte continue event:  [op] [payload] [delay_lo] [delay_hi]
2-byte LAST event:      [op | 0x80] [payload]
                        (note: for kbd press LAST and mouse-move LAST,
                         the wine app uses 4-byte form with payload+delay)
```

The opcode byte is a bitfield:

```
bit 0x80 — this is the LAST event in the macro
bit 0x40 — direction: press / "set" (vs release / "clear")
bits 3..0 — event type:
  0x2 = keyboard event
  0x3 = mouse-button event
  0x6 = mouse-move event
```

Concrete opcodes:

| Hex  | Meaning                            |
|------|------------------------------------|
| 0x42 | keyboard press, more events follow |
| 0xc2 | keyboard press, LAST event         |
| 0x02 | keyboard release, more follow      |
| 0x82 | keyboard release, LAST             |
| 0x43 | mouse button press, more follow    |
| 0xc3 | mouse button press, LAST           |
| 0x03 | mouse button release, more follow  |
| 0x83 | mouse button release, LAST         |
| 0x46 | mouse move, more follow            |
| 0xc6 | mouse move, LAST                   |

### Event payloads

**Keyboard event (`0x42` / `0x82` / `0x02` / `0xc2`)**:
```
[op] [HID usage code] [delay_lo] [delay_hi]
```
Delay is post-event delay before the next event, LE 16-bit milliseconds.

**Mouse-button event (`0x43` / `0x83` / `0x03` / `0xc3`)**:
```
[op] [button mask] [delay_lo] [delay_hi]
```
Button mask is a bitfield: `0x01` left, `0x02` right, `0x04` middle.

**Mouse-move event (`0x46` / `0xc6`)**:

Continue (4 bytes):
```
[op] [packed_xy] [post_delay] [direction_flags]
```
- `packed_xy`: high nibble = X magnitude (0..15), low nibble = Y magnitude
- `direction_flags`:
  - bit `0x40` — X positive (right); clear = X negative (left)
  - bit `0x80` — Y positive (down); clear = Y negative (up)
- `post_delay`: ms before next event

LAST move (2 bytes):
```
[0xc6] [packed_xy]
```
The 2-byte LAST format has no slot for direction flags, so the firmware
falls back to the default direction (likely positive). Practical
implication: don't end a macro on a "left" or "up" move — put it earlier
in the chain and end on a positive-direction move.

## Macro mode buffer (cmd `0x09 0xa5`)

The mode buffer is paginated. Page 0 carries per-button mode records;
pages 1..4 carry DPI levels, polling rate, and RGB profile data.

### Page 0 layout (48 bytes, padded)

A list of 4-byte tagged records:

```
20 01 00 00          — left click (always default)
20 02 00 00          — right click (default) OR replaced when reconfigured
20 04 00 00          — middle click (default) OR replaced when reconfigured
70 IDX cnt mode       — side button mode record (one per button)
70 IDX cnt mode       —   ...
21 55 00 00          — RGB record (preserved verbatim)
21 38 01 00          — RGB record
21 38 ff 00          — RGB record
70 IDX cnt mode       — more side button records
...
```

The `70 IDX REPEAT MODE` records hold per-side-button macro mode info.
**`IDX` is NOT in alphabetical button order** — it's permuted in the
firmware. Captures show:

| `IDX` value | Side button name |
|-------------|------------------|
| `0`         | A                |
| `1`         | C                |
| `2`         | B                |
| `3`         | D                |
| `4`         | E                |
| `5`         | F                |

`MODE` byte values:

| Byte | UI label                       | `COUNT` semantics              |
|------|--------------------------------|--------------------------------|
| 0x02 | "Loop until stop pressing"     | always 1 (default for buttons) |
| 0x03 | "Single fire" (Loop Times = 1) | always 1                       |
| 0x00 | "Loops until Any Click"        | when `count == 1`              |
| 0x00 | "Loop Times" N                 | when `count >= 2` — N times    |

The wine app uses two different mode bytes for "Loop Times = 1" (`0x03`)
vs "Loops until Any Click" (`0x00, count=1`) so the firmware can
distinguish them despite the count being identical.

### Right/middle/DPI button assignments — open question

When the user reconfigures the right click, middle click, or DPI button
in the wine app, additional `70`-records appear in the page-0 buffer
(replacing the `20 02` / `20 04` placeholders). Their `IDX` values
collide with side-button IDX values, so position-in-buffer presumably
disambiguates which physical button each record controls. We have not
fully isolated this mapping and the tool currently leaves these buttons
on whatever the wine app last wrote.

The actual letter assignment for the main buttons does NOT appear in
the keymap section we decoded — they must be stored in another buffer
section we haven't fully isolated.

## DPI / RGB sections (cmds `0x0f 0xae`, `0x22 0xae`, `0x23 0xae`)

Capture-only; we send these verbatim from a known-good wine Apply.

The `0x0f 0xae` write carries DPI levels (5 levels), polling rate,
color values, and a few reserved fields.

The `0x22 0xae` and `0x23 0xae` writes carry small (3-byte payload)
auxiliary RGB profile updates.

## Apply sequence (the order the wine app sends frames)

1. Read phase — reads existing config from the device:
   - `cmd 0x30 0xa5` (state read?)
   - `cmd 0x08 0xa5` pages 0..5 (mode buffer read)
   - `cmd 0x0e 0xa5` (DPI/RGB read)
   - `cmd 0x03 0x00` (device identification)
2. Write phase:
   - `cmd 0x0d 0x00` partition table (offset 0)
   - `cmd 0x0d 0x00` keymap (offset 0x38, more frames if slots are larger)
   - `cmd 0x10 0xa5` marker (no payload)
   - `cmd 0x09 0xa5` pages 0..4 (mode buffer write)
   - `cmd 0x0f 0xae` DPI/polling/RGB
   - `cmd 0x22 0xae` RGB
   - `cmd 0x23 0xae` RGB

Each transaction is acknowledged by the device with an `aa`-prefixed
echo. There's no explicit "commit" — onboard flash is updated as each
frame is received.

## Things still unknown

- The exact algorithm by which the wine app computes the byte-3 "checksum"
  values. Doesn't matter for writing (firmware doesn't validate) but would
  be useful to fully match the wine app's frames byte-for-byte.
- The right-click / middle-click / DPI button keystroke storage location.
- The semantics of the `0a 00` vs `08 00` per-button slot header.
- The semantics of the various `20 XX 00 00` and `21 XX YY 00` records
  in the mode buffer (RGB / wheel / DPI default action?).
- Pages 1..4 of the mode buffer (DPI/polling/RGB) — preserved verbatim
  rather than understood.
- The `cmd 0xfa 0xae` 8-byte response — looks like a serial number but
  not parsed.

## Sibling models

Other MKESPN-brand mice using the YJX-CHIP controller (USB VID `a8a4`)
should use the same protocol with potentially different button counts
or memory layout. The X16 wired mouse, despite being from the same
brand, uses a DIFFERENT chip (USB VID `30fa`, "INSTANT") with a
totally different protocol — only IN endpoints, no vendor OUT channel,
config presumably via control transfers (HID `SET_REPORT`). Out of
scope for this tool.

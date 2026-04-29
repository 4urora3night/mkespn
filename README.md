# mkespn — Linux programmer for MKESPN W12 / YJX-CHIP USB mice

CLI tool that programs the macro buttons of MKESPN-brand mice that use the
**YJX-CHIP** USB controller (USB VID `a8a4`, PID `2255` — the W12 model and
likely sibling models). Replaces the Windows-only manufacturer config app
(SXS-W12M, "DeviceDriver.exe") so you can configure your mouse from Linux
without booting Windows or running the app under Wine every time.

> Programs onboard memory of the mouse — once written, the configuration
> persists across reboots, USB reconnects, and on any host OS (Linux,
> Windows, macOS, Android via OTG, etc.).

The protocol was reverse-engineered from USB captures of the manufacturer's
Windows app running under wine-staging 11.7. See
[`PROTOCOL.md`](PROTOCOL.md) for the full decode.

## What it can do

- Program the **6 side buttons** (labelled A..F in the wine UI) to:
  - Single keyboard letter / function key (any HID usage code)
  - One of four macro modes:
    - `ptt` — fires while button is physically held (push-to-talk style)
    - `single` — one keypress per click regardless of hold duration
    - `until-click` — repeats indefinitely until any other button is pressed
    - `loop:N` — fires the macro N times
- Read the device's identification (model, firmware build date)
- Read the current macro-mode buffer from the device (debug)

## What it can't do (yet)

- Right-click / Middle-click / DPI-button reassignment — these live in a
  config section we haven't isolated. They keep whatever you last set in
  the wine app.
- Multi-event macros (chord sequences, key-then-mouse, mouse moves) — the
  encoding is decoded (see `PROTOCOL.md`) but no CLI surface exposed yet;
  modify a `Macro` programmatically if you need them today.
- DPI / polling-rate / RGB editing — bytes preserved verbatim from a
  captured default.

## Requirements

- Linux with `hidraw` (any modern kernel)
- Python 3.8+
- Read/write access to `/dev/hidraw*` for vendor `a8a4` (see Setup)

## Setup

Grant non-root access to the W12's HID devices via udev:

```bash
sudo tee /etc/udev/rules.d/99-mkespn-w12.rules <<'EOF'
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="a8a4", ATTRS{idProduct}=="2255", \
  MODE="0660", GROUP="plugdev", TAG+="uaccess"
EOF

sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw
```

Then unplug and replug the mouse, or simply re-trigger as above.

## Usage

```bash
# Show what's saved locally (the tool maintains state in ~/.config/mkespn/w12.json)
python3 w12.py show

# Read the device's identification string
python3 w12.py info

# Restore default state (a/b/c/d/e/f all in PTT mode)
python3 w12.py reset

# Set side button A to F13, push-to-talk style
# (perfect for Android Key Mapper PTT — F13 doesn't print as text)
python3 w12.py set A f13 ptt

# Set side button B to '1', firing exactly once per click
python3 w12.py set B 1 single

# Set side button C to '2', firing 5 times per click
python3 w12.py set C 2 loop:5

# Set side button D to "any", repeating forever until another button is pressed
python3 w12.py set D q until-click

# Push the current state to the device
python3 w12.py apply
```

### Mode reference

| Mode flag      | UI option in wine app           | Wire `(MODE, COUNT)` |
|----------------|--------------------------------|----------------------|
| `ptt`          | "Loop until stop pressing"     | `(0x02, 1)`          |
| `single`       | "Loop Times" with N=1          | `(0x03, 1)`          |
| `until-click`  | "Loops until Any Click"        | `(0x00, 1)`          |
| `loop:N`       | "Loop Times" with N≥2          | `(0x00, N)`          |

### Supported keys

Letters `a..z`, digits `0..9`, `enter`, `escape`, `backspace`, `tab`, `space`,
`f1..f24`. For PTT use cases on Android (where the action shouldn't insert text
into focused fields) pick `f13..f24` — those don't have default OS bindings.

Pass any HID Usage ID directly as a hex int if you need exotic keys
(see USB HID 1.11 §10).

## Physical button layout (W12)

The W12 has 3 buttons on each side. Pressing them in physical order from the
left-side top-to-bottom and then right-side top-to-bottom produces:

```
Left top    → side button A
Left mid    → side button B
Left bot    → side button C
Right top   → side button F
Right mid   → side button E
Right bot   → side button D
```

(Right side is reverse alphabetical because of the wine UI's mouse-diagram
labelling — left side goes A→B→C, right side goes F→E→D from front to back.)

## Files

- `w12.py` — the entire tool (single Python file, no third-party deps)
- `PROTOCOL.md` — protocol reverse-engineering notes
- `~/.config/mkespn/w12.json` — your persistent local state (created on first run)

## License

MIT.

## Origin

Reverse-engineered in a single session by capturing USB traffic from the
manufacturer's Windows configurator running under wine-staging.
The W12 stores all macros in onboard flash, so once programmed the mouse
behaves identically across all OSes — making this tool a one-shot
replacement for the manufacturer software regardless of where you use the
mouse afterwards.

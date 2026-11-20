# Current installation update — 2026-11-12

All installed strips are now assumed 10 physical LEDs longer than the previous model.
180 strips: 10,440 physical LEDs / 10,260 usable LEDs. First physical LED always OFF.
RGB input uint8 shape (12,16,65,3); RGB565 input uint16 shape (12,16,65).
Visible input j=0 is physical LED 2. RGB565/LED2 payload 24,960 bytes + 20-byte header.

## Color correction

Corrections belong to the PC sender, not firmware. `send_numpy.CentralUSB.prepare`
copies the RGB data, swaps R/G for configured strips, then maps Zone to DD and packs RGB565.
Producer data stays ordinary RGB. Do not pre-swap it again. Blue is unchanged.
Logical previews retain intended physical colors. Direct DD mode (`zones=False`) bypasses corrections.
Saved configuration: `color_corrections.json`. Only the modular RGB565 sender has been updated;
old ZIPs, single-file handoff, and pinmask firmware/client remain historical snapshots.

| Position | Zone | C port |
|---|---:|---:|
| M1 | 6 | 13 |
| M2 | 6 | 14 |
| M6 | 11 | 11 |
| N1 | 6 | 15 |
| N5 | 5 | 10 |
| N6 | 5 | 12 |
| N7 | 4 | 8 |
| N8 | 4 | 6 |
| N10 | 3 | 8 |
| O1 | 5 | 8 |
| O2 | 5 | 6 |
| O4 | 5 | 9 |
| O7 | 4 | 7 |
| O9 | 4 | 9 |
| O10 | 4 | 10 |
| O11 | 4 | 12 |
| P1 | 5 | 7 |
| P2 | 5 | 3 |
| P3 | 5 | 1 |
| P4 | 5 | 14 |
| P5 | 5 | 16 |
| P6 | 4 | 4 |
| P7 | 4 | 2 |
| P8 | 4 | 13 |
| P9 | 4 | 15 |
| P10 | 4 | 11 |
| Q1 | 5 | 4 |
| Q2 | 5 | 2 |
| Q3 | 5 | 13 |
| Q4 | 5 | 15 |
| Q6 | 4 | 3 |
| Q7 | 4 | 1 |
| Q8 | 4 | 14 |
| Q9 | 4 | 16 |

## Firmware and playback

New binary: `firmware/central_usb_rgb565_plus10.bin`. Build record: `firmware/lighting_build_plus10.json`.
Old firmware binaries retained. Prior source/mapping snapshots: `archive/before_plus10_2026-11-12/`.
Firmware max_physical=66 / max_visible=65; CPU240MHz; brightness32; wireless OFF.
Row physical counts: A66 B65 C64 D63 E62 F61 G60 H59; I absent; J57 K56 L55 M54 N53 O52 P51 Q50.
Canonical layout JSON, mapping/ports/modules CSV and two diagrams regenerated.
The plane angle stays 28.758918 degrees; model raises anchors by 50 to keep bottoms at height50.

`play_blue_rain.py --port /dev/cu.usbmodem... --fps 8` plays white heads with cyan/blue tails.
Data is masked to current row counts. Ctrl+C sends final black.
RGB test: `check_rgb_order.py --port ...`; fixed green: append `--color green`.
Earlier fixed-55 pattern scripts need porting before reuse with the new layout.

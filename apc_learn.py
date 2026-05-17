#!/usr/bin/env python3
"""
APC Key 25 Pad Learning  —  run ONCE before starting midi-hub
=============================================================
Guides you through pressing every pad on the 5×8 grid.
  Columns 0-3 → instrument clip pads
  Column  4   → scene launch buttons (one per row)

Saves ~/apc_map.json which midi_hub.py reads on startup.

Usage:
    python3 /home/tasso/apc_learn.py
"""

import rtmidi
import json
import os
import sys
import time

ROWS      = 8
COLS      = 5       # 4 instrument cols + 1 scene launch col

COL_NAMES = ["T-8 Drums", "T-8 Bass", "UNO Synth", "E-4", "SCENE LAUNCH"]
MAP_PATH  = os.path.expanduser("~/apc_map.json")

LED_WAITING = 4   # red blink  — "press me now"
LED_DONE    = 1   # green      — "recorded, move on"
LED_OFF     = 0

NOTE_ON  = 0x90
NOTE_OFF = 0x80


def find_port(port_list, fragment):
    fl = fragment.lower()
    for i, name in enumerate(port_list):
        if fl in name.lower():
            return i
    return None


def main():
    pi = rtmidi.MidiIn()
    po = rtmidi.MidiOut()
    ins  = pi.get_ports(); del pi
    outs = po.get_ports(); del po

    in_idx  = find_port(ins,  "APC Key 25")
    out_idx = find_port(outs, "APC Key 25")
    if in_idx is None or out_idx is None:
        print("ERROR: APC Key 25 not found. Plug it in and try again.")
        sys.exit(1)

    midi_in = rtmidi.MidiIn()
    midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
    midi_in.open_port(in_idx)

    midi_out = rtmidi.MidiOut()
    midi_out.open_port(out_idx)

    # Clear all LEDs
    for note in range(128):
        midi_out.send_message([NOTE_ON, note, LED_OFF])
    time.sleep(0.05)

    print("=" * 60)
    print("APC Key 25  —  Pad Learning")
    print("=" * 60)
    print(f"You will press {ROWS * COLS} pads in order.")
    print("  Row 0 = TOP row    Row 7 = BOTTOM row")
    print("  Col 0 = LEFT col   Col 4 = RIGHT col (scene launch)")
    print()
    print("Press pads when prompted. Hold nothing — just tap each one.")
    print("=" * 60)
    print()

    grid      = [[None] * COLS for _ in range(ROWS)]
    seen      = set()
    prev_note = None

    def wait_unique_note_on():
        """Block until a fresh Note On arrives (not already mapped)."""
        while True:
            event = midi_in.get_message()
            if event:
                msg, _ = event
                if len(msg) >= 3 and (msg[0] & 0xF0) == NOTE_ON and msg[2] > 0:
                    note = msg[1]
                    if note in seen:
                        print(f"\n  (note {note} already used — press a DIFFERENT pad)",
                              end=" ", flush=True)
                        continue
                    return note
            time.sleep(0.008)

    for r in range(ROWS):
        for c in range(COLS):
            label = COL_NAMES[c]
            prompt = f"Row {r}, Col {c}  [{label}]"
            print(f"  Press  {prompt}:", end=" ", flush=True)

            note = wait_unique_note_on()
            grid[r][c] = note
            seen.add(note)

            # Light previous pad green, current waiting
            if prev_note is not None:
                midi_out.send_message([NOTE_ON, prev_note, LED_DONE])
            midi_out.send_message([NOTE_ON, note, LED_DONE])
            prev_note = note
            print(f"note {note}")

    # Light last pad green
    if prev_note is not None:
        midi_out.send_message([NOTE_ON, prev_note, LED_DONE])

    # ── Overlap check ─────────────────────────────────────────────────────────
    print()
    all_pad_notes = {grid[r][c] for r in range(ROWS) for c in range(COLS)}
    # Typical 25-key keyboard default range: C2-C4 (36-60) or C3-C5 (48-72)
    keyboard_range = set(range(36, 97))
    overlap = sorted(all_pad_notes & keyboard_range)
    if overlap:
        print("WARNING: pad notes overlap with typical keyboard range:")
        print(f"  Overlapping notes: {overlap}")
        print("  Shift the APC keyboard OCTAVE UP (hold Shift + press Oct+)")
        print("  until the keyboard range is above note 96.")
        print("  This prevents keyboard notes triggering pad actions.")
    else:
        print("No overlap between pad notes and keyboard range. Good.")

    # ── Save ──────────────────────────────────────────────────────────────────
    data = {"clip_notes": grid}
    with open(MAP_PATH, "w") as f:
        json.dump(data, f, indent=2)

    print()
    print(f"Saved to {MAP_PATH}")
    print()
    print("Learned grid (row=scene, col=instrument/scene):")
    col_header = "  " + "  ".join(f"C{c}" for c in range(COLS))
    print(col_header)
    for r, row in enumerate(grid):
        print(f"  R{r}: {row}")
    print()
    print("Columns: T-8 Drums | T-8 Bass | UNO Synth | E-4 | SCENE")
    print()
    print("Done. Start the hub with:")
    print("  sudo systemctl start midi-hub")

    time.sleep(0.5)
    for note in range(128):
        midi_out.send_message([NOTE_ON, note, LED_OFF])

    midi_in.close_port()
    midi_out.close_port()


if __name__ == "__main__":
    main()

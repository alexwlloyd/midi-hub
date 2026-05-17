#!/usr/bin/env python3
"""
APC Key 25  —  Instrument Select Button Learning
=================================================
Maps 4 buttons to the 4 instrument columns:
  Col 0  T-8 Drums
  Col 1  T-8 Bass
  Col 2  UNO Synth
  Col 3  E-4

Appends "instrument_select_notes" to ~/apc_map.json.

Usage:
    python3 /home/tasso/apc_learn_select.py
"""

import rtmidi
import json
import os
import sys
import time

MAP_PATH  = os.path.expanduser("~/apc_map.json")
COL_NAMES = ["T-8 Drums", "T-8 Bass", "UNO Synth", "E-4"]

LED_WAITING = 4   # red blink
LED_DONE    = 1   # green solid
LED_OFF     = 0


def find_port(port_list, fragment):
    fl = fragment.lower()
    for i, name in enumerate(port_list):
        if fl in name.lower():
            return i
    return None


def main():
    if not os.path.exists(MAP_PATH):
        print(f"ERROR: {MAP_PATH} not found. Run apc_learn.py first.")
        sys.exit(1)

    with open(MAP_PATH) as f:
        data = json.load(f)

    # Load existing clip notes so we can reject already-mapped notes
    existing = set()
    for row in data.get("clip_notes", []):
        for n in row:
            if n is not None:
                existing.add(n)
    for n in data.get("instrument_select_notes", []):
        if n is not None:
            existing.add(n)

    pi = rtmidi.MidiIn()
    po = rtmidi.MidiOut()
    ins  = pi.get_ports(); del pi
    outs = po.get_ports(); del po

    in_idx  = find_port(ins,  "APC Key 25")
    out_idx = find_port(outs, "APC Key 25")
    if in_idx is None or out_idx is None:
        print("ERROR: APC Key 25 not found.")
        sys.exit(1)

    midi_in = rtmidi.MidiIn()
    midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
    midi_in.open_port(in_idx)

    midi_out = rtmidi.MidiOut()
    midi_out.open_port(out_idx)

    print("=" * 56)
    print("APC Key 25  —  Instrument Select Button Learning")
    print("=" * 56)
    print("Press the 4 buttons you want to use for live keyboard")
    print("instrument selection, in this order:")
    for i, name in enumerate(COL_NAMES):
        print(f"  {i+1}. {name}")
    print()
    print("These should be DIFFERENT from the 5x8 clip pad grid.")
    print("=" * 56)
    print()

    sel_notes = []
    seen      = set()

    def wait_unique_note_on():
        while True:
            event = midi_in.get_message()
            if event:
                msg, _ = event
                if len(msg) >= 3 and (msg[0] & 0xF0) == 0x90 and msg[2] > 0:
                    note = msg[1]
                    if note in existing or note in seen:
                        print(f"\n  (note {note} already mapped — press a different button)",
                              end=" ", flush=True)
                        continue
                    return note
            time.sleep(0.008)

    for i, name in enumerate(COL_NAMES):
        print(f"  Press button for  [{name}]:", end=" ", flush=True)
        note = wait_unique_note_on()
        sel_notes.append(note)
        seen.add(note)
        midi_out.send_message([0x90, note, LED_DONE])
        print(f"note {note}")

    data["instrument_select_notes"] = sel_notes

    with open(MAP_PATH, "w") as f:
        json.dump(data, f, indent=2)

    print()
    print(f"Saved to {MAP_PATH}")
    print()
    print("Instrument select buttons:")
    for i, (name, note) in enumerate(zip(COL_NAMES, sel_notes)):
        print(f"  Col {i}  {name:<12}  note {note}")
    print()
    print("Restart the hub:")
    print("  sudo systemctl start midi-hub")

    time.sleep(0.5)
    for n in sel_notes:
        midi_out.send_message([0x90, n, LED_OFF])

    midi_in.close_port()
    midi_out.close_port()


if __name__ == "__main__":
    main()

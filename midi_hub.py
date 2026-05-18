#!/usr/bin/env python3
"""
MIDI Hub + Clip Launcher  —  Raspberry Pi 5, headless
======================================================

Grid: 4 instrument cols × 8 scene rows  (+ col 4 = scene launch)

  Col 0  T-8 Drums   MIDI ch10   APC 25-key keyboard → T-8
  Col 1  T-8 Bass    MIDI ch 2   APC 25-key keyboard → T-8
  Col 2  UNO Synth   MIDI ch 1   UNO Synth keyboard  → UNO Synth
  Col 3  E-4         MIDI ch 1   APC 25-key keyboard → E-4
  Col 4  Scene launch (one button per row)

APC keyboard default when nothing is recording: live to the selected instrument.
Instrument select buttons (notes from apc_map.json "instrument_select_notes"):
  Sel 0  T-8 Drums
  Sel 1  T-8 Bass
  Sel 2  UNO Synth
  Sel 3  E-4         (default on startup)
Pressing a select button switches live keyboard routing immediately.
Recording overrides this temporarily; routing returns to selected target after stop.

Clip state machine per slot
  EMPTY      -> [press]            -> ARMED_REC
  ARMED_REC  -> [next bar bound.]  -> RECORDING
  RECORDING  -> [press]            -> ARMED_STOP
  ARMED_STOP -> [next bar bound.]  -> PLAYING  (loop length quantized to bars)
  PLAYING    -> [press]            -> PAUSED
  PAUSED     -> [press]            -> PLAYING  (resumes from bar 0)
  Any        -> [hold >=1.5 s]     -> EMPTY   (clears clip)

Scene launch (col 4 buttons): plays / re-triggers all loaded clips in that row.

Clock source: UNO Synth MIDI clock (0xF8), forwarded to E-4 and T-8.
Quantization: bar boundaries -- 4 beats x 24 MIDI ticks = 96 ticks per bar.

Pad mapping: loaded from ~/apc_map.json (run apc_learn.py once to generate).

LED colours on APC Key 25
  Off         = empty slot
  Red blink   = armed for record  (waiting for next bar)
  Red solid   = recording
  Green blink = armed to stop     (waiting for next bar)
  Green solid = playing
  Yellow      = paused
  Green blink = scene button with at least one loaded clip
"""

import rtmidi
import time
import threading
import logging
import random
import sys
import json
import os
from enum import IntEnum
from dataclasses import dataclass, field

# --- Logging ------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# --- Timing constants ---------------------------------------------------------

STARTUP_DELAY    = 60          # seconds to wait on boot for USB to settle
CLOCK_WATCHDOG   = 5           # seconds without clock before reconnect
MIDI_CLOCK       = 0xF8
MIDI_START       = 0xFA
TICKS_PER_BEAT   = 24
BEATS_PER_BAR    = 4
TICKS_PER_BAR    = TICKS_PER_BEAT * BEATS_PER_BAR    # 96 ticks = 1 bar (4/4)
TICKS_PER_32ND   = TICKS_PER_BEAT // 8               #  3 ticks = 1 thirty-second note
HOLD_THRESHOLD   = 1.5         # seconds: hold pad to clear clip

# --- Arpeggiator --------------------------------------------------------------
# Knob CC numbers (APC Key 25 knobs 1-3, ch1, CC 48-50)
ARP_CC_RATE      = 48
ARP_CC_OCTAVES   = 49
ARP_CC_DIRECTION = 50
ARP_CC_GATE      = 51   # knob 4: note gate 20-99% of step length

# Rate: CC value → ticks per arp step (0 = off)
ARP_RATES      = [0, TICKS_PER_BEAT, TICKS_PER_BEAT//2,
                  TICKS_PER_BEAT//4, TICKS_PER_BEAT//8]  # off, 1/4, 1/8, 1/16, 1/32
ARP_RATE_NAMES = ["off", "1/4", "1/8", "1/16", "1/32"]

# Direction names (9 total, evenly spaced across 0-127)
ARP_DIRECTIONS = [
    "up", "down", "updown", "downup",
    "updowndown", "downupup", "updownup", "downupdown", "random",
]

# --- Grid dimensions ----------------------------------------------------------

INSTRUMENT_COLS = 4    # cols 0-3 hold clips
SCENE_COL       = 4    # col 4 = scene launch
ROWS            = 8

# --- Column routing -----------------------------------------------------------
# src: "apc" = APC 25-key keyboard   "uno" = UNO Synth keyboard
# ch:  0-indexed MIDI channel (ch=9 -> MIDI channel 10, etc.)

COLUMNS = [
    {"name": "T-8 Drums",  "out": "T-8",       "ch": 9,  "src": "apc"},
    {"name": "T-8 Bass",   "out": "T-8",       "ch": 1,  "src": "apc"},
    {"name": "UNO Synth",  "out": "UNO Synth", "ch": 0,  "src": "uno"},
    {"name": "E-4",        "out": "E-4",       "ch": 1,  "src": "apc"},  # APC keyboard sends on ch2 (index 1)
]

# --- APC Key 25 pad map -------------------------------------------------------

MAP_PATH   = os.path.expanduser("~/apc_map.json")
CLIPS_PATH = os.path.expanduser("~/clips.json")

# Fallback defaults (APC Key 25 standard MIDI implementation).
# note = 56 - (row x 7) + col  for cols 0-4
_DEFAULT_GRID = [[56 - r * 7 + c for c in range(INSTRUMENT_COLS + 1)] for r in range(ROWS)]

MASTER_PP_DEFAULT_NOTE = 81   # fallback if not in apc_map.json

def _load_map():
    sel_notes   = [None, None, None, None]
    master_note = MASTER_PP_DEFAULT_NOTE
    if os.path.exists(MAP_PATH):
        with open(MAP_PATH) as f:
            data = json.load(f)
        grid        = data["clip_notes"]
        sel_notes   = data.get("instrument_select_notes", sel_notes)
        master_note = data.get("master_play_pause_note", MASTER_PP_DEFAULT_NOTE)
        log.info("Loaded APC pad map from %s", MAP_PATH)
        if any(n is None for n in sel_notes):
            log.warning("instrument_select_notes missing — run apc_learn_select.py")
        if "master_play_pause_note" not in data:
            log.warning("master_play_pause_note not in map — using default note %d", master_note)
    else:
        grid = _DEFAULT_GRID
        log.warning(
            "No APC map at %s -- using defaults. Run apc_learn.py first.", MAP_PATH
        )
    return grid, sel_notes, master_note

# --- LED velocity codes (APC Key 25 / APC mini style) ------------------------

class LED(IntEnum):
    OFF          = 0
    GREEN        = 1   # PLAYING
    GREEN_BLINK  = 2   # ARMED_STOP or scene has content
    RED          = 3   # RECORDING
    RED_BLINK    = 4   # ARMED_REC
    YELLOW       = 5   # PAUSED
    YELLOW_BLINK = 6   # (spare)

# --- Clip state machine -------------------------------------------------------

class State(IntEnum):
    EMPTY         = 0
    ARMED_REC     = 1
    RECORDING     = 2
    ARMED_STOP    = 3
    PLAYING       = 4
    PAUSED        = 5
    MASTER_PAUSED = 6   # paused by master stop; queued to resume on master play

STATE_LED = {
    State.EMPTY:         LED.OFF,
    State.ARMED_REC:     LED.RED_BLINK,
    State.RECORDING:     LED.RED,
    State.ARMED_STOP:    LED.GREEN_BLINK,
    State.PLAYING:       LED.GREEN,
    State.PAUSED:        LED.YELLOW,
    State.MASTER_PAUSED: LED.GREEN_BLINK,   # flash green: will resume on master play
}

@dataclass
class Clip:
    state:        State = State.EMPTY
    events:       list  = field(default_factory=list)
    # events = [(tick_offset: int, msg: list), ...]
    # tick_offset is relative to clip start; msg has channel already remapped.
    loop_ticks:   int   = 0      # total loop length in ticks
    play_pos:     int   = 0      # current playback position within loop (ticks)
    rec_start:    int   = 0      # abs_tick when recording began
    press_time:   float = 0.0    # wall-clock time of last pad press (hold detection)
    active_notes: set   = field(default_factory=set)  # notes held during recording

    def reset(self):
        self.state        = State.EMPTY
        self.events       = []
        self.loop_ticks   = 0
        self.play_pos     = 0
        self.active_notes = set()

# --- Port utilities -----------------------------------------------------------

REQUIRED_INPUTS  = ["UNO Synth", "APC Key 25"]
REQUIRED_OUTPUTS = ["T-8", "E-4", "UNO Synth", "APC Key 25"]

def _find_port(port_list, fragment):
    fl = fragment.lower()
    for i, name in enumerate(port_list):
        if fl in name.lower():
            return i
    return None

def _wait_for_ports(timeout=60, interval=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pi = rtmidi.MidiIn();  ins  = pi.get_ports(); del pi
        po = rtmidi.MidiOut(); outs = po.get_ports(); del po
        missing = (
            [n for n in REQUIRED_INPUTS  if _find_port(ins,  n) is None] +
            [n for n in REQUIRED_OUTPUTS if _find_port(outs, n) is None]
        )
        if not missing:
            log.info("All MIDI ports ready.")
            return ins, outs
        log.info("Waiting for ports: %s", missing)
        time.sleep(interval)
    raise RuntimeError("Timed out waiting for MIDI ports.")

# --- Hub ----------------------------------------------------------------------

class MidiHub:

    def __init__(self):
        self.lock        = threading.Lock()
        self.clips       = [[Clip() for _ in range(INSTRUMENT_COLS)] for _ in range(ROWS)]
        self.abs_tick    = 0
        self.last_clock  = None
        self.grid           = None   # 8x5 note grid
        self.note_to_pos    = {}     # note -> (row, col)  col==SCENE_COL -> scene launch
        self.sel_notes       = [None, None, None, None]  # instrument select button notes
        self.sel_note_to_col = {}    # note -> col index (0-3)
        self.keyboard_target = 3     # col index keyboard plays live to (default: E-4)
        self.held_apc_notes  = set() # APC keyboard notes currently held down
        self.held_uno_notes  = set() # UNO Synth keyboard notes currently held down
        self.master_paused   = False # True when master stop has been issued
        self.master_pp_note  = MASTER_PP_DEFAULT_NOTE  # button note for master play/pause
        self._save_needed    = False # set by MIDI callbacks; checked in run loop

        # Arpeggiator state
        self.arp_ticks_per_step = 0            # 0 = off
        self.arp_octaves        = 1            # 1–3: note range in octaves
        self.arp_direction      = "up"
        self.arp_held_notes     = {}           # {note: velocity} currently held on APC kbd
        self.arp_sequence       = []           # expanded step list (rebuilt on change)
        self.arp_pos            = 0            # current step index
        self.arp_tick_counter   = 0            # ticks since last arp step
        self.arp_gate_pct       = 0.75         # 0.20–0.99: fraction of step note sounds for
        self.arp_current_note   = None         # (note, col) currently sounding, or None
        self._rng               = random.Random()

        self.out_t8   = None
        self.out_e4   = None
        self.out_uno  = None
        self.out_apc  = None   # to APC for LED feedback
        self.in_uno   = None
        self.in_apc   = None

    # -- Map -------------------------------------------------------------------

    def _build_note_map(self):
        self.grid, self.sel_notes, self.master_pp_note = _load_map()
        self.note_to_pos = {}
        for r, row in enumerate(self.grid):
            for c, note in enumerate(row):
                if note is not None:
                    self.note_to_pos[note] = (r, c)
        self.sel_note_to_col = {
            note: col
            for col, note in enumerate(self.sel_notes)
            if note is not None
        }
        log.info("Instrument select notes: %s", self.sel_notes)
        log.info("Master play/pause note: %s", self.master_pp_note)

    # -- Port management -------------------------------------------------------

    def _open_ports(self, ins, outs):
        def mk_out(name):
            idx = _find_port(outs, name)
            m = rtmidi.MidiOut()
            m.open_port(idx)
            log.info("  OUT  %s", outs[idx])
            return m

        def mk_in(name, cb, ignore_timing=True):
            idx = _find_port(ins, name)
            m = rtmidi.MidiIn()
            m.ignore_types(sysex=True, timing=ignore_timing, active_sense=True)
            m.open_port(idx)
            m.set_callback(cb)
            log.info("  IN   %s", ins[idx])
            return m

        self.out_t8  = mk_out("T-8")
        self.out_e4  = mk_out("E-4")
        self.out_uno = mk_out("UNO Synth")
        self.out_apc = mk_out("APC Key 25")
        self.in_uno  = mk_in("UNO Synth",  self._cb_uno, ignore_timing=False)
        self.in_apc  = mk_in("APC Key 25", self._cb_apc, ignore_timing=True)

        self.out_e4.send_message([MIDI_START])
        self.out_t8.send_message([MIDI_START])
        log.info("Sent MIDI Start to E-4 and T-8.")
        self._clear_all_leds()
        self._update_select_leds()
        self._load_clips()

    def _close(self):
        for m in (self.in_uno, self.in_apc,
                  self.out_t8, self.out_e4, self.out_uno, self.out_apc):
            if m:
                try:
                    m.close_port()
                except Exception:
                    pass
        (self.in_uno, self.in_apc,
         self.out_t8, self.out_e4, self.out_uno, self.out_apc) = [None] * 6
        self.last_clock = None

    # -- LED helpers -----------------------------------------------------------

    def _led(self, row, col, vel):
        if self.out_apc and self.grid:
            note = self.grid[row][col]
            self.out_apc.send_message([0x90, note, int(vel)])

    def _clear_all_leds(self):
        if not self.out_apc or not self.grid:
            return
        for r in range(ROWS):
            for c in range(INSTRUMENT_COLS + 1):
                self._led(r, c, LED.OFF)
        for note in self.sel_notes:
            if note is not None:
                self.out_apc.send_message([0x90, note, LED.OFF])
        if self.master_pp_note is not None:
            self.out_apc.send_message([0x90, self.master_pp_note, LED.OFF])

    def _update_select_leds(self):
        """Light the active instrument select button; dim the others."""
        if not self.out_apc:
            return
        for col, note in enumerate(self.sel_notes):
            if note is not None:
                vel = LED.GREEN_BLINK if col == self.keyboard_target else LED.OFF
                self.out_apc.send_message([0x90, note, int(vel)])

    def _select_keyboard_target(self, col):
        """Switch the live keyboard target to instrument column col."""
        self.keyboard_target = col
        self._update_select_leds()
        log.info("  Keyboard target -> Col %d  %s", col, COLUMNS[col]["name"])

    def _update_scene_led(self, row):
        has_content = any(
            self.clips[row][c].state != State.EMPTY
            for c in range(INSTRUMENT_COLS)
        )
        self._led(row, SCENE_COL, LED.GREEN_BLINK if has_content else LED.OFF)

    # -- Output routing helpers ------------------------------------------------

    def _out_for_col(self, col):
        return {
            "T-8":       self.out_t8,
            "E-4":       self.out_e4,
            "UNO Synth": self.out_uno,
        }[COLUMNS[col]["out"]]

    def _remap_ch(self, msg, col):
        target = COLUMNS[col]["ch"]
        return [msg[0] & 0xF0 | target] + list(msg[1:])

    def _all_notes_off(self, col):
        out = self._out_for_col(col)
        if out:
            ch = COLUMNS[col]["ch"]
            out.send_message([0xB0 | ch, 123, 0])

    # -- Master play / pause ---------------------------------------------------

    def _master_pause(self):
        """Stop all instruments and freeze all playing clips (green-blink = will resume)."""
        self.master_paused = True
        for out in (self.out_t8, self.out_e4, self.out_uno):
            if out:
                out.send_message([0xFC])   # MIDI Stop
        for r in range(ROWS):
            for c in range(INSTRUMENT_COLS):
                clip = self.clips[r][c]
                if clip.state == State.PLAYING:
                    self._all_notes_off(c)
                    clip.state = State.MASTER_PAUSED
                    self._led(r, c, LED.GREEN_BLINK)
        if self.master_pp_note is not None and self.out_apc:
            self.out_apc.send_message([0x90, self.master_pp_note, LED.YELLOW])
        self._schedule_save()
        log.info("Master PAUSE — %d clip(s) queued for resume", sum(
            1 for r in range(ROWS) for c in range(INSTRUMENT_COLS)
            if self.clips[r][c].state == State.MASTER_PAUSED
        ))

    def _master_play(self):
        """Resume all instruments and restart all MASTER_PAUSED clips."""
        self.master_paused = False
        for out in (self.out_t8, self.out_e4, self.out_uno):
            if out:
                out.send_message([0xFB])   # MIDI Continue
        resumed = 0
        for r in range(ROWS):
            for c in range(INSTRUMENT_COLS):
                clip = self.clips[r][c]
                if clip.state == State.MASTER_PAUSED:
                    clip.state = State.PLAYING
                    self._led(r, c, LED.GREEN)
                    resumed += 1
        if self.master_pp_note is not None and self.out_apc:
            self.out_apc.send_message([0x90, self.master_pp_note, LED.OFF])
        self._schedule_save()
        log.info("Master PLAY — %d clip(s) resumed", resumed)

    # -- Clip persistence ------------------------------------------------------

    def _schedule_save(self):
        """Request a clip save. Actual write happens in the run loop (outside callbacks)."""
        self._save_needed = True

    def _save_clips(self):
        """Write all completed clips to CLIPS_PATH atomically."""
        entries = []
        for r in range(ROWS):
            for c in range(INSTRUMENT_COLS):
                clip = self.clips[r][c]
                # Only persist completed clips (skip transient recording states)
                if clip.state in (State.EMPTY, State.ARMED_REC,
                                  State.RECORDING, State.ARMED_STOP):
                    continue
                entries.append({
                    "row":        r,
                    "col":        c,
                    "state":      "playing" if clip.state == State.PLAYING else "paused",
                    "loop_ticks": clip.loop_ticks,
                    "events":     [[t, msg] for t, msg in clip.events],
                })
        tmp = CLIPS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"clips": entries}, f)
        os.replace(tmp, CLIPS_PATH)
        log.info("Saved %d clip(s) → %s", len(entries), CLIPS_PATH)

    def _load_clips(self):
        """Restore clips from CLIPS_PATH. All restored clips start PAUSED."""
        if not os.path.exists(CLIPS_PATH):
            log.info("No saved clips at %s — starting fresh.", CLIPS_PATH)
            return
        try:
            with open(CLIPS_PATH) as f:
                data = json.load(f)
        except Exception as e:
            log.warning("Could not load clips (%s) — starting fresh.", e)
            return

        count = 0
        for entry in data.get("clips", []):
            r, c = entry.get("row"), entry.get("col")
            if r is None or c is None:
                continue
            if not (0 <= r < ROWS and 0 <= c < INSTRUMENT_COLS):
                continue
            clip = self.clips[r][c]
            clip.loop_ticks = entry["loop_ticks"]
            clip.events     = [(e[0], e[1]) for e in entry["events"]]
            clip.play_pos   = 0
            clip.state      = State.PAUSED
            self._led(r, c, LED.YELLOW)
            self._update_scene_led(r)
            count += 1

        log.info("Loaded %d clip(s) from %s — all PAUSED, press master play to start.",
                 count, CLIPS_PATH)

    # -- Arpeggiator -----------------------------------------------------------

    def _arp_build_sequence(self):
        """Rebuild arp_sequence from held notes, octave range and direction.
        Call while holding self.lock (or before ports open)."""
        notes = sorted(self.arp_held_notes.keys())
        if not notes:
            self.arp_sequence = []
            return

        # Expand across octave range
        expanded = [min(127, n + oct * 12)
                    for oct in range(self.arp_octaves)
                    for n in notes]
        n = len(expanded)
        up   = expanded[:]
        down = expanded[::-1]

        d = self.arp_direction
        if d == "up":
            seq = up
        elif d == "down":
            seq = down
        elif d == "updown":
            # ping-pong, no endpoint repeat: C E G E
            seq = up + (down[1:-1] if n > 2 else down[1:])
        elif d == "downup":
            seq = down + (up[1:-1] if n > 2 else up[1:])
        elif d == "updowndown":
            # up, then full down (top note repeated at turnaround): C E G G E C
            seq = up + down
        elif d == "downupup":
            # down, then full up (bottom note repeated): G E C C E G
            seq = down + up
        elif d == "updownup":
            # three-phase: up → down → up (inner extremes only): C E G E C E G …
            seq = up + down[1:] + up[1:]
        elif d == "downupdown":
            seq = down + up[1:] + down[1:]
        else:  # random — sequence is just the pool; step picks randomly
            seq = expanded[:]

        self.arp_sequence = seq
        if seq:
            self.arp_pos = self.arp_pos % len(seq)

    def _arp_note_off(self):
        """Silence the currently sounding arp note and record note-off if needed.
        Lock must be held. Called both from the gate tick and from other contexts."""
        if self.arp_current_note is None:
            return
        note, col = self.arp_current_note
        out = self._out_for_col(col)
        if out:
            out.send_message([0x80 | COLUMNS[col]["ch"], note, 0])
        # Record note-off into any active clip
        rec = self._active_apc_recording()
        if rec:
            r, c = rec
            clip = self.clips[r][c]
            t    = round((self.abs_tick - clip.rec_start) / TICKS_PER_32ND) * TICKS_PER_32ND
            clip.events.append((t, [0x80 | COLUMNS[c]["ch"], note, 0]))
            clip.active_notes.discard(note)
        self.arp_current_note = None

    def _arp_step(self):
        """Fire the next arp note. Note-off is handled by the gate tick in _tick().
        Lock must be held."""
        # Gate should already have silenced the previous note. Safety fallback:
        if self.arp_current_note is not None:
            self._arp_note_off()

        if not self.arp_held_notes or not self.arp_sequence:
            return

        # Decide output column (recording clip takes priority over keyboard_target)
        rec = self._active_apc_recording()
        col = rec[1] if rec else self.keyboard_target

        # Pick next note
        if self.arp_direction == "random":
            next_note = self._rng.choice(self.arp_sequence)
        else:
            self.arp_pos %= len(self.arp_sequence)
            next_note    = self.arp_sequence[self.arp_pos]
            self.arp_pos = (self.arp_pos + 1) % len(self.arp_sequence)

        # Velocity: match pitch class back to a held note
        base = next_note % 12
        vel  = next((v for n, v in self.arp_held_notes.items() if n % 12 == base), 100)

        # Send note-on
        ch  = COLUMNS[col]["ch"]
        out = self._out_for_col(col)
        if out:
            out.send_message([0x90 | ch, next_note, vel])
        if rec:
            r, c = rec
            clip = self.clips[r][c]
            t    = round((self.abs_tick - clip.rec_start) / TICKS_PER_32ND) * TICKS_PER_32ND
            clip.events.append((t, [0x90 | COLUMNS[c]["ch"], next_note, vel]))
            clip.active_notes.add(next_note)

        self.arp_current_note = (next_note, col)

    # -- Clock / sequencer tick ------------------------------------------------

    def _tick(self):
        """Called on every 0xF8 clock pulse, inside self.lock."""
        self.abs_tick += 1

        # Arpeggiator — gate fires note-off early; step fires next note-on
        if self.arp_ticks_per_step > 0:
            self.arp_tick_counter += 1
            gate_tick = max(1, int(self.arp_ticks_per_step * self.arp_gate_pct))
            if self.arp_tick_counter == gate_tick:
                self._arp_note_off()
            if self.arp_tick_counter >= self.arp_ticks_per_step:
                self.arp_tick_counter = 0
                self._arp_step()

        if self.abs_tick % TICKS_PER_BEAT == 0:
            self._beat_boundary()

        for r in range(ROWS):
            for c in range(INSTRUMENT_COLS):
                clip = self.clips[r][c]
                if clip.state != State.PLAYING or clip.loop_ticks == 0:
                    continue
                out = self._out_for_col(c)
                for ev_tick, ev_msg in clip.events:
                    if ev_tick == clip.play_pos:
                        out.send_message(ev_msg)
                clip.play_pos += 1
                if clip.play_pos >= clip.loop_ticks:
                    clip.play_pos = 0

    def _beat_boundary(self):
        for r in range(ROWS):
            for c in range(INSTRUMENT_COLS):
                clip = self.clips[r][c]

                if clip.state == State.ARMED_REC:
                    clip.state        = State.RECORDING
                    clip.rec_start    = self.abs_tick
                    clip.events       = []
                    clip.active_notes = set()
                    # Inject Note Ons for any keys already held at the moment recording begins
                    ch = COLUMNS[c]["ch"]
                    held = self.held_uno_notes if c == 2 and self.keyboard_target != 2 else self.held_apc_notes
                    for hn in held:
                        clip.events.append((0, [0x90 | ch, hn, 64]))
                        clip.active_notes.add(hn)
                    self._led(r, c, LED.RED)
                    log.info("  [%d,%d] %s  RECORDING started  (pre-held: %d)",
                             r, c, COLUMNS[c]["name"], len(held))

                elif clip.state == State.ARMED_STOP:
                    raw   = self.abs_tick - clip.rec_start
                    steps = max(1, round(raw / TICKS_PER_BEAT))
                    clip.loop_ticks = steps * TICKS_PER_BEAT
                    ch = COLUMNS[c]["ch"]
                    for note in clip.active_notes:
                        clip.events.append((clip.loop_ticks - 1, [0x80 | ch, note, 0]))
                    clip.active_notes = set()
                    clip.play_pos     = 0
                    if self.master_paused:
                        clip.state = State.MASTER_PAUSED
                        self._led(r, c, LED.GREEN_BLINK)
                    else:
                        clip.state = State.PLAYING
                        self._led(r, c, LED.GREEN)
                    self._update_scene_led(r)
                    self._schedule_save()
                    log.info("  [%d,%d] %s  %s  loop=%d beats  events=%d",
                             r, c, COLUMNS[c]["name"],
                             "MASTER_PAUSED" if self.master_paused else "PLAYING",
                             steps, len(clip.events))

    # -- Clip pad actions ------------------------------------------------------

    def _active_apc_recording(self):
        """Return (row, col) of any clip currently recording via APC keyboard, or None.
        Includes UNO Synth column when keyboard_target points to it."""
        for r in range(ROWS):
            for c in range(INSTRUMENT_COLS):
                if self.clips[r][c].state == State.RECORDING:
                    if COLUMNS[c]["src"] == "apc" or c == self.keyboard_target:
                        return (r, c)
        return None

    def _apc_would_record(self, col):
        """True if arming col would use the APC keyboard as input."""
        return COLUMNS[col]["src"] == "apc" or col == self.keyboard_target

    def _press_clip(self, row, col):
        clip = self.clips[row][col]

        if clip.state == State.EMPTY:
            if self._apc_would_record(col) and self._active_apc_recording():
                log.info("  [%d,%d] blocked -- another APC clip is already recording", row, col)
                return
            clip.state = State.ARMED_REC
            self._led(row, col, LED.RED_BLINK)
            log.info("  [%d,%d] %s  ARMED_REC", row, col, COLUMNS[col]["name"])

        elif clip.state == State.ARMED_REC:
            clip.state = State.EMPTY
            self._led(row, col, LED.OFF)
            self._update_scene_led(row)
            log.info("  [%d,%d] arm cancelled", row, col)

        elif clip.state == State.RECORDING:
            clip.state = State.ARMED_STOP
            self._led(row, col, LED.GREEN_BLINK)
            log.info("  [%d,%d] ARMED_STOP", row, col)

        elif clip.state == State.ARMED_STOP:
            clip.state = State.RECORDING
            self._led(row, col, LED.RED)
            log.info("  [%d,%d] stop cancelled -> RECORDING", row, col)

        elif clip.state == State.PLAYING:
            self._all_notes_off(col)
            clip.state = State.PAUSED
            self._led(row, col, LED.YELLOW)
            self._schedule_save()
            log.info("  [%d,%d] PAUSED", row, col)

        elif clip.state == State.PAUSED:
            if self.master_paused:
                # In master-pause mode: queue this clip for resume (flash green)
                clip.state = State.MASTER_PAUSED
                self._led(row, col, LED.GREEN_BLINK)
                self._schedule_save()
                log.info("  [%d,%d] queued for master resume", row, col)
            else:
                clip.play_pos = 0
                clip.state    = State.PLAYING
                self._led(row, col, LED.GREEN)
                self._schedule_save()
                log.info("  [%d,%d] PLAYING (resumed)", row, col)

        elif clip.state == State.MASTER_PAUSED:
            # Remove from master resume list -> ordinary pause (yellow)
            clip.state = State.PAUSED
            self._led(row, col, LED.YELLOW)
            self._schedule_save()
            log.info("  [%d,%d] removed from master resume -> PAUSED", row, col)

    def _clear_clip(self, row, col):
        if self.clips[row][col].state != State.EMPTY:
            self._all_notes_off(col)
            self.clips[row][col].reset()
            self._led(row, col, LED.OFF)
            self._update_scene_led(row)
            self._schedule_save()
            log.info("  [%d,%d] EMPTY (cleared by hold)", row, col)

    def _exclusive_play(self, active_row, col):
        """Pause any other PLAYING clip in the same column (one clip per instrument)."""
        for r in range(ROWS):
            if r == active_row:
                continue
            clip = self.clips[r][col]
            if clip.state == State.PLAYING:
                self._all_notes_off(col)
                clip.state = State.PAUSED
                self._led(r, col, LED.YELLOW)

    def _launch_scene(self, row):
        triggered = 0
        for c in range(INSTRUMENT_COLS):
            clip = self.clips[row][c]
            if clip.state == State.PLAYING:
                clip.play_pos = 0
                triggered += 1
            elif clip.state == State.PAUSED:
                clip.play_pos = 0
                clip.state    = State.PLAYING
                self._led(row, c, LED.GREEN)
                triggered += 1
        self._led(row, SCENE_COL, LED.GREEN_BLINK)
        self._schedule_save()
        log.info("  Scene %d launched  (%d clip(s))", row, triggered)

    # -- MIDI callbacks --------------------------------------------------------

    def _cb_uno(self, event, data=None):
        msg, _ = event
        if not msg:
            return
        status = msg[0]

        if status == MIDI_CLOCK:
            with self.lock:
                self.last_clock = time.time()
                self.out_e4.send_message(msg)
                self.out_t8.send_message(msg)
                self._tick()
            return

        high = status & 0xF0
        if high not in (0x80, 0x90, 0xA0, 0xB0, 0xC0, 0xD0, 0xE0):
            return   # drop START/STOP/CONTINUE from UNO Synth

        with self.lock:
            rec_row = next(
                (r for r in range(ROWS) if self.clips[r][2].state == State.RECORDING),
                None
            )
        # Track held UNO keys for held-note injection at record start
        if high == 0x90 and len(msg) > 2 and msg[2] > 0:
            self.held_uno_notes.add(msg[1])
        elif high == 0x80 or (high == 0x90 and (len(msg) < 3 or msg[2] == 0)):
            self.held_uno_notes.discard(msg[1])

        with self.lock:
            rec_row = next(
                (r for r in range(ROWS) if self.clips[r][2].state == State.RECORDING),
                None
            )
            if rec_row is not None:
                clip     = self.clips[rec_row][2]
                raw      = self.abs_tick - clip.rec_start
                tick_off = round(raw / TICKS_PER_32ND) * TICKS_PER_32ND
                stored   = self._remap_ch(msg, 2)
                clip.events.append((tick_off, stored))
                if high == 0x90 and len(msg) > 2 and msg[2] > 0:
                    clip.active_notes.add(msg[1])
                elif high == 0x80 or (high == 0x90 and (len(msg) < 3 or msg[2] == 0)):
                    clip.active_notes.discard(msg[1])
                # No echo-back: UNO sounds its own keys internally during record.
                # Playback (when PLAYING) is handled by _tick().

    def _cb_apc(self, event, data=None):
        msg, _ = event
        if not msg:
            return
        status     = msg[0]
        high       = status & 0xF0
        ch_idx     = status & 0x0F
        note       = msg[1] if len(msg) > 1 else 0
        vel        = msg[2] if len(msg) > 2 else 0
        # APC Key 25: pads/buttons send on ch1 (index 0), keyboard on ch2 (index 1).
        # Distinguish by channel so overlapping note numbers don't trigger pad
        # actions from keyboard presses.
        is_keyboard = (ch_idx == 1)

        if not is_keyboard:
            # -- Instrument select buttons --------------------------------------
            # Consume ALL events from select buttons (both press and release)
            # so they never leak into keyboard routing.
            sel_col = self.sel_note_to_col.get(note)
            if sel_col is not None:
                if high == 0x90 and vel > 0:
                    with self.lock:
                        self._select_keyboard_target(sel_col)
                return

        # -- Master play / pause button ----------------------------------------
        if not is_keyboard and note == self.master_pp_note:
            if high == 0x90 and vel > 0:
                with self.lock:
                    if self.master_paused:
                        self._master_play()
                    else:
                        self._master_pause()
            return

        # -- Pad / scene button ------------------------------------------------
        pos = self.note_to_pos.get(note)
        if not is_keyboard and high in (0x80, 0x90) and pos is not None:
            row, col = pos
            with self.lock:
                if high == 0x90 and vel > 0:
                    if col == SCENE_COL:
                        self._launch_scene(row)
                    else:
                        self.clips[row][col].press_time = time.time()
                else:
                    if col != SCENE_COL:
                        clip = self.clips[row][col]
                        held = time.time() - clip.press_time
                        if held >= HOLD_THRESHOLD:
                            self._clear_clip(row, col)
                        else:
                            self._press_clip(row, col)
            return

        # -- 25-key keyboard ---------------------------------------------------
        if high not in (0x80, 0x90, 0xA0, 0xB0, 0xC0, 0xD0, 0xE0):
            return

        # -- Arpeggiator knob CC messages (ch1, knobs 1-3) ---------------------
        if high == 0xB0 and not is_keyboard:
            if note == ARP_CC_RATE:
                rate_idx = min(4, vel * 5 // 128)
                with self.lock:
                    self.arp_ticks_per_step = ARP_RATES[rate_idx]
                    self.arp_tick_counter   = 0
                    if self.arp_ticks_per_step == 0:
                        self._arp_note_off()
                        self.arp_held_notes.clear()
                        self.arp_sequence = []
                log.info("Arp rate -> %s", ARP_RATE_NAMES[rate_idx])
                return
            if note == ARP_CC_OCTAVES:
                with self.lock:
                    self.arp_octaves = max(1, min(3, vel * 3 // 128 + 1))
                    self._arp_build_sequence()
                log.info("Arp octaves -> %d", self.arp_octaves)
                return
            if note == ARP_CC_DIRECTION:
                dir_idx = min(8, vel * 9 // 128)
                with self.lock:
                    self.arp_direction = ARP_DIRECTIONS[dir_idx]
                    self.arp_pos = 0
                    self._arp_build_sequence()
                log.info("Arp direction -> %s", self.arp_direction)
                return
            if note == ARP_CC_GATE:
                # Map CC 0-127 → gate 20%-99%
                pct = 0.20 + (vel / 127.0) * 0.79
                with self.lock:
                    self.arp_gate_pct = pct
                log.info("Arp gate -> %d%%", int(pct * 100))
                return
            # Other knob CCs: fall through and forward to target instrument

        # Track held keys for held-note injection at record start
        if high == 0x90 and vel > 0:
            self.held_apc_notes.add(note)
        elif high == 0x80 or vel == 0:
            self.held_apc_notes.discard(note)

        # -- Arp active: intercept note on/off, let _arp_step handle output ----
        if high in (0x80, 0x90) and self.arp_ticks_per_step > 0:
            with self.lock:
                if high == 0x90 and vel > 0:
                    self.arp_held_notes[note] = vel
                else:
                    self.arp_held_notes.pop(note, None)
                self._arp_build_sequence()
                # If all notes released, silence current arp note
                if not self.arp_held_notes:
                    self._arp_note_off()
            return

        # -- Normal routing (arp off) ------------------------------------------
        with self.lock:
            rec = self._active_apc_recording()
            if rec:
                r, c     = rec
                clip     = self.clips[r][c]
                stored   = self._remap_ch(msg, c)
                raw      = self.abs_tick - clip.rec_start
                tick_off = round(raw / TICKS_PER_32ND) * TICKS_PER_32ND
                clip.events.append((tick_off, stored))
                if high == 0x90 and vel > 0:
                    clip.active_notes.add(note)
                elif high == 0x80 or vel == 0:
                    clip.active_notes.discard(note)
                self._out_for_col(c).send_message(stored)
            else:
                # Live keyboard to selected target (default: E-4)
                stored = self._remap_ch(msg, self.keyboard_target)
                self._out_for_col(self.keyboard_target).send_message(stored)

    # -- Main run loop ---------------------------------------------------------

    def run(self):
        log.info("Startup: waiting %ds for USB devices to settle...", STARTUP_DELAY)
        time.sleep(STARTUP_DELAY)

        self._build_note_map()

        # Start web UI once (stays up across MIDI reconnects)
        try:
            import sys, os
            sys.path.insert(0, os.path.expanduser("~"))
            import web_server
            web_server.start(self)
        except Exception as e:
            log.warning("Web UI failed to start: %s", e)

        while True:
            try:
                ins, outs = _wait_for_ports(timeout=60)
                self._open_ports(ins, outs)

                log.info("Clip launcher active:")
                for c, col in enumerate(COLUMNS):
                    log.info("  Col %d: %-12s  ch%-2d  src=%s",
                             c, col["name"], col["ch"] + 1, col["src"])
                log.info("  Col 4: Scene launch")
                log.info("  Quantization: %d/4  (%d ticks/bar)",
                         BEATS_PER_BAR, TICKS_PER_BAR)
                log.info("  Hold %.1fs to clear a clip.", HOLD_THRESHOLD)

                while True:
                    time.sleep(1)
                    if self._save_needed:
                        self._save_needed = False
                        self._save_clips()
                    if (self.last_clock is not None and
                            time.time() - self.last_clock > CLOCK_WATCHDOG):
                        raise RuntimeError(
                            "No UNO Synth clock for %ds -- reconnecting..." % CLOCK_WATCHDOG
                        )

            except KeyboardInterrupt:
                log.info("Shutting down.")
                self._close()
                break
            except Exception as e:
                log.error("%s", e)
                self._close()
                time.sleep(5)


if __name__ == "__main__":
    MidiHub().run()

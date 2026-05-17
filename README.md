# midi-hub

Raspberry Pi 4 MIDI clip launcher and router, controlled from an Akai APC Key 25.

## Hardware

| Device | Role |
|---|---|
| Akai APC Key 25 | Grid controller + 25-key keyboard |
| Roland T-8 | Drums (ch 10) + Bass (ch 2) |
| IK Multimedia UNO Synth | MIDI clock source + instrument col 2 |
| Elektron E-4 | Vocoder / synth, default APC keyboard target |

## Features

- **4 × 8 clip grid** — 4 instrument columns × 8 scene rows
- **32nd-note quantization** — record start/stop and note positions snap to 32nd-note grid
- **Scene launch** — col 4 buttons retrigger / resume all clips in a row simultaneously
- **Instrument select buttons** — switch live APC keyboard target between instruments
- **Arpeggiator** — rate, octave range, direction, and gate via APC knobs 1–4 (CC 48–51)
- **Master play/pause** — single button freezes / resumes all clips + sends MIDI Stop/Continue
- **Clip persistence** — clips auto-save to `~/clips.json`; restored as Paused on reboot
- **Web UI** — Flask app on port 8080: view grid, press/clear clips, save/load loops and songs, upload MIDI files
- **Auto-reconnect** — detects missing MIDI clock and reconnects on device unplug/replug

## Clip State Machine

```
EMPTY → [press] → ARMED_REC → [next 32nd] → RECORDING
RECORDING → [press] → ARMED_STOP → [next 32nd] → PLAYING
PLAYING → [press] → PAUSED → [press] → PLAYING
Any → [hold 1.5 s] → EMPTY
```

## LED Colours (APC Key 25 pads)

| Colour | State |
|---|---|
| Off | Empty |
| Red blink | Armed for record |
| Red solid | Recording |
| Green blink | Armed to stop / scene has content / master-paused will resume |
| Green solid | Playing |
| Yellow | Paused |

## Files

| File | Purpose |
|---|---|
| `midi_hub.py` | Main daemon — clip launcher, router, arpeggiator |
| `web_server.py` | Flask web UI (started by midi_hub.py on boot) |
| `apc_learn.py` | One-time pad grid learning (run once, creates `apc_map.json`) |
| `apc_learn_select.py` | One-time instrument-select button learning (appends to `apc_map.json`) |
| `apc_map.json` | Learned note map (example — re-generate with learn scripts) |
| `midi-hub.service` | systemd unit file |

## Setup

### 1. Install dependencies

```bash
sudo apt install python3-rtmidi python3-mido python3-flask
```

### 2. Learn the APC pad layout (once)

```bash
sudo systemctl stop midi-hub   # if already running
python3 apc_learn.py           # press all 40 pads in order
python3 apc_learn_select.py    # press the 4 instrument-select buttons
```

> **Tip:** Use APC Shift + Oct+ to shift the keyboard range above note 96 so keyboard notes don't collide with pad notes.

### 3. Install the service

```bash
sudo cp midi_hub.py /usr/local/bin/midi_hub.py
sudo cp web_server.py /home/tasso/web_server.py   # loaded from ~ at runtime
sudo cp midi-hub.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable midi-hub
sudo systemctl start midi-hub
```

### 4. Watch logs

```bash
sudo journalctl -u midi-hub -f
```

### 5. Web UI

Open `http://<raspberry-pi-ip>:8080` in a browser.

## Column Routing

| Col | Instrument | MIDI ch | Input |
|---|---|---|---|
| 0 | T-8 Drums | 10 | APC keyboard |
| 1 | T-8 Bass | 2 | APC keyboard |
| 2 | UNO Synth | 1 | UNO keyboard (or APC when sel=2) |
| 3 | E-4 | 2 | APC keyboard (default) |
| 4 | Scene launch | — | — |

## Arpeggiator (APC knobs 1–4)

| Knob | CC | Function | Range |
|---|---|---|---|
| 1 | 48 | Rate | off / 1/4 / 1/8 / 1/16 / 1/32 |
| 2 | 49 | Octave range | 1 / 2 / 3 |
| 3 | 50 | Direction | up / down / updown / downup / … / random |
| 4 | 51 | Gate | 20 % – 99 % |

## Clock

The UNO Synth is the MIDI clock master (`0xF8`). The hub forwards clock to the E-4 and T-8. If no clock is received for 5 seconds the hub closes and reopens all MIDI ports automatically.

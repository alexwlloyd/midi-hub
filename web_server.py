"""
MIDI Hub Web UI — Flask server
Runs in a background daemon thread inside the midi_hub.py process.
"""

import json
import logging
import os
import threading

log = logging.getLogger(__name__)

BANKS_DIR = os.path.expanduser("~/banks")
SONGS_DIR = os.path.expanduser("~/songs")
WEB_PORT  = 8080


# ---------------------------------------------------------------------------
# MIDI file import
# ---------------------------------------------------------------------------

def _midi_to_clip(path, ticks_per_beat=24, ticks_per_32nd=3, ticks_per_bar=96):
    """Parse a MIDI file and return (events, loop_ticks) in internal format."""
    try:
        import mido
    except ImportError:
        raise RuntimeError("mido not installed — run: sudo apt install python3-mido")

    mid = mido.MidiFile(path)
    scale = ticks_per_beat / mid.ticks_per_beat

    events = []
    max_tick = 0
    for track in mid.tracks:
        tick = 0
        for msg in track:
            tick += msg.time
            if msg.type in ("note_on", "note_off"):
                q = round(tick * scale / ticks_per_32nd) * ticks_per_32nd
                max_tick = max(max_tick, q)
                if msg.type == "note_on" and msg.velocity > 0:
                    events.append([q, [0x90, msg.note, msg.velocity]])
                else:
                    events.append([q, [0x80, msg.note, 0]])

    events.sort(key=lambda e: e[0])
    raw = max_tick + ticks_per_32nd
    loop_ticks = max(ticks_per_bar,
                     ((raw + ticks_per_bar - 1) // ticks_per_bar) * ticks_per_bar)
    return events, loop_ticks


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

def start(hub):
    """Create the Flask app and launch it in a background daemon thread."""
    os.makedirs(os.path.join(BANKS_DIR, "loops"), exist_ok=True)
    os.makedirs(SONGS_DIR, exist_ok=True)

    try:
        from flask import Flask, jsonify, request
    except ImportError:
        log.warning("Flask not installed — web UI disabled.")
        return

    # Import constants from the running __main__ module (midi_hub.py is __main__)
    import sys as _sys
    _m = _sys.modules.get("__main__") or _sys.modules.get("midi_hub")
    State          = _m.State
    COLUMNS        = _m.COLUMNS
    LED            = _m.LED
    ROWS           = _m.ROWS
    INSTRUMENT_COLS = _m.INSTRUMENT_COLS

    app = Flask(__name__)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def clip_dict(r, c, clip):
        return {
            "row": r, "col": c,
            "state": clip.state.name,
            "loop_ticks": clip.loop_ticks,
            "events_count": len(clip.events),
            "col_name": COLUMNS[c]["name"],
        }

    def atomic_write(path, data):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    # -----------------------------------------------------------------------
    # UI
    # -----------------------------------------------------------------------

    @app.route("/")
    def index():
        return HTML_UI

    # -----------------------------------------------------------------------
    # Grid state
    # -----------------------------------------------------------------------

    @app.route("/api/state")
    def api_state():
        with hub.lock:
            clips = [clip_dict(r, c, hub.clips[r][c])
                     for r in range(ROWS)
                     for c in range(INSTRUMENT_COLS)]
            master = hub.master_paused
            kt = hub.keyboard_target
        return jsonify({
            "clips": clips,
            "master_paused": master,
            "keyboard_target": kt,
            "col_names": [col["name"] for col in COLUMNS],
        })

    # -----------------------------------------------------------------------
    # Clip actions
    # -----------------------------------------------------------------------

    @app.route("/api/clip/<int:row>/<int:col>", methods=["POST"])
    def api_clip_action(row, col):
        if not (0 <= row < ROWS and 0 <= col < INSTRUMENT_COLS):
            return jsonify({"error": "out of range"}), 400
        action = (request.json or {}).get("action", "press")
        with hub.lock:
            if action == "press":
                hub._press_clip(row, col)
            elif action == "clear":
                hub._clear_clip(row, col)
        return jsonify({"ok": True})

    @app.route("/api/clip/<int:row>/<int:col>/save", methods=["POST"])
    def api_save_clip(row, col):
        if not (0 <= row < ROWS and 0 <= col < INSTRUMENT_COLS):
            return jsonify({"error": "out of range"}), 400
        body = request.json or {}
        name = body.get("name", "clip").strip()
        bank = body.get("bank", "loops").strip()
        if not name or ".." in name or "/" in name:
            return jsonify({"error": "invalid name"}), 400

        with hub.lock:
            clip = hub.clips[row][col]
            if clip.state in (State.EMPTY, State.ARMED_REC,
                              State.RECORDING, State.ARMED_STOP):
                return jsonify({"error": "Clip not complete"}), 400
            data = {
                "name": name,
                "col": col,
                "col_name": COLUMNS[col]["name"],
                "loop_ticks": clip.loop_ticks,
                "events": [[t, list(msg)] for t, msg in clip.events],
            }

        bank_path = os.path.join(BANKS_DIR, bank)
        os.makedirs(bank_path, exist_ok=True)
        atomic_write(os.path.join(bank_path, name + ".json"), data)
        return jsonify({"ok": True, "saved_as": name, "bank": bank})

    # -----------------------------------------------------------------------
    # Loop banks
    # -----------------------------------------------------------------------

    @app.route("/api/banks")
    def api_banks():
        result = {}
        if not os.path.isdir(BANKS_DIR):
            return jsonify(result)
        for bank in sorted(os.listdir(BANKS_DIR)):
            bp = os.path.join(BANKS_DIR, bank)
            if not os.path.isdir(bp):
                continue
            clips = []
            for fn in sorted(os.listdir(bp)):
                if not fn.endswith(".json"):
                    continue
                clip_name = fn[:-5]
                try:
                    with open(os.path.join(bp, fn)) as f:
                        d = json.load(f)
                    clips.append({
                        "name": clip_name,
                        "col_name": d.get("col_name", ""),
                        "loop_ticks": d.get("loop_ticks", 0),
                        "events_count": len(d.get("events", [])),
                    })
                except Exception:
                    clips.append({"name": clip_name, "col_name": "",
                                  "loop_ticks": 0, "events_count": 0})
            result[bank] = clips
        return jsonify(result)

    @app.route("/api/banks/<bank>/<name>/load", methods=["POST"])
    def api_load_clip(bank, name):
        if ".." in bank or "/" in bank or ".." in name or "/" in name:
            return jsonify({"error": "invalid path"}), 400
        body = request.json or {}
        row, col = body.get("row"), body.get("col")
        if row is None or col is None:
            return jsonify({"error": "row and col required"}), 400
        if not (0 <= row < ROWS and 0 <= col < INSTRUMENT_COLS):
            return jsonify({"error": "out of range"}), 400

        path = os.path.join(BANKS_DIR, bank, name + ".json")
        if not os.path.exists(path):
            return jsonify({"error": "not found"}), 404

        with open(path) as f:
            data = json.load(f)

        ch = COLUMNS[col]["ch"]
        remapped = [
            [t, [(msg[0] & 0xF0) | ch] + msg[1:]]
            for t, msg in data["events"]
        ]

        with hub.lock:
            clip = hub.clips[row][col]
            if clip.state not in (State.EMPTY, State.PAUSED, State.MASTER_PAUSED):
                return jsonify({"error": "Slot is busy (recording or playing)"}), 409
            if clip.state != State.EMPTY:
                hub._all_notes_off(col)
            clip.loop_ticks = data["loop_ticks"]
            clip.events     = [tuple(e) for e in remapped]
            clip.play_pos   = 0
            clip.state      = State.PAUSED
            hub._led(row, col, LED.YELLOW)
            hub._update_scene_led(row)
            hub._schedule_save()

        return jsonify({"ok": True, "loaded": name, "row": row, "col": col})

    @app.route("/api/banks/<bank>/<name>", methods=["DELETE"])
    def api_delete_clip(bank, name):
        if ".." in bank or "/" in bank or ".." in name or "/" in name:
            return jsonify({"error": "invalid path"}), 400
        path = os.path.join(BANKS_DIR, bank, name + ".json")
        if not os.path.exists(path):
            return jsonify({"error": "not found"}), 404
        os.remove(path)
        return jsonify({"ok": True})

    # -----------------------------------------------------------------------
    # Songs (full-grid snapshots)
    # -----------------------------------------------------------------------

    @app.route("/api/songs")
    def api_songs():
        songs = []
        if os.path.isdir(SONGS_DIR):
            for fn in sorted(os.listdir(SONGS_DIR)):
                if fn.endswith(".json"):
                    try:
                        with open(os.path.join(SONGS_DIR, fn)) as f:
                            d = json.load(f)
                        songs.append({"name": fn[:-5],
                                      "clips_count": len(d.get("clips", []))})
                    except Exception:
                        songs.append({"name": fn[:-5], "clips_count": 0})
        return jsonify(songs)

    @app.route("/api/songs", methods=["POST"])
    def api_save_song():
        name = (request.json or {}).get("name", "song").strip()
        if not name or ".." in name or "/" in name:
            return jsonify({"error": "invalid name"}), 400

        with hub.lock:
            clips = []
            for r in range(ROWS):
                for c in range(INSTRUMENT_COLS):
                    clip = hub.clips[r][c]
                    if clip.state in (State.EMPTY, State.ARMED_REC,
                                      State.RECORDING, State.ARMED_STOP):
                        continue
                    clips.append({
                        "row": r, "col": c,
                        "col_name": COLUMNS[c]["name"],
                        "loop_ticks": clip.loop_ticks,
                        "events": [[t, list(msg)] for t, msg in clip.events],
                    })

        if not clips:
            return jsonify({"error": "No completed clips to save"}), 400

        atomic_write(os.path.join(SONGS_DIR, name + ".json"),
                     {"name": name, "clips": clips})
        return jsonify({"ok": True, "name": name, "clips_count": len(clips)})

    @app.route("/api/songs/<name>/load", methods=["POST"])
    def api_load_song(name):
        if ".." in name or "/" in name:
            return jsonify({"error": "invalid name"}), 400
        path = os.path.join(SONGS_DIR, name + ".json")
        if not os.path.exists(path):
            return jsonify({"error": "not found"}), 404

        with open(path) as f:
            data = json.load(f)

        with hub.lock:
            for r in range(ROWS):
                for c in range(INSTRUMENT_COLS):
                    if hub.clips[r][c].state != State.EMPTY:
                        hub._all_notes_off(c)
                        hub.clips[r][c].reset()
                        hub._led(r, c, LED.OFF)
            for entry in data.get("clips", []):
                r, c = entry["row"], entry["col"]
                if not (0 <= r < ROWS and 0 <= c < INSTRUMENT_COLS):
                    continue
                ch = COLUMNS[c]["ch"]
                remapped = [
                    [t, [(msg[0] & 0xF0) | ch] + msg[1:]]
                    for t, msg in entry["events"]
                ]
                clip = hub.clips[r][c]
                clip.loop_ticks = entry["loop_ticks"]
                clip.events     = [tuple(e) for e in remapped]
                clip.play_pos   = 0
                clip.state      = State.PAUSED
                hub._led(r, c, LED.YELLOW)
                hub._update_scene_led(r)
            hub._schedule_save()

        return jsonify({"ok": True, "loaded": name,
                        "clips_count": len(data.get("clips", []))})

    @app.route("/api/songs/<name>", methods=["DELETE"])
    def api_delete_song(name):
        if ".." in name or "/" in name:
            return jsonify({"error": "invalid path"}), 400
        path = os.path.join(SONGS_DIR, name + ".json")
        if not os.path.exists(path):
            return jsonify({"error": "not found"}), 404
        os.remove(path)
        return jsonify({"ok": True})

    # -----------------------------------------------------------------------
    # MIDI upload
    # -----------------------------------------------------------------------

    @app.route("/api/upload", methods=["POST"])
    def api_upload():
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "no file"}), 400
        fname = f.filename or "upload.mid"
        if not fname.lower().endswith((".mid", ".midi")):
            return jsonify({"error": "must be a .mid file"}), 400

        safe = fname.replace(" ", "_").replace("..", "_")
        tmp  = os.path.join("/tmp", safe)
        f.save(tmp)
        try:
            events, loop_ticks = _midi_to_clip(tmp)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

        clip_name = os.path.splitext(safe)[0]
        atomic_write(os.path.join(BANKS_DIR, "loops", clip_name + ".json"), {
            "name": clip_name, "col": 0, "col_name": "imported",
            "loop_ticks": loop_ticks, "events": events,
        })
        return jsonify({"ok": True, "name": clip_name,
                        "loop_ticks": loop_ticks, "events_count": len(events)})

    # -----------------------------------------------------------------------
    # Master play/pause
    # -----------------------------------------------------------------------

    @app.route("/api/master", methods=["POST"])
    def api_master():
        action = (request.json or {}).get("action")
        with hub.lock:
            if action == "pause":
                hub._master_pause()
            elif action == "play":
                hub._master_play()
        return jsonify({"ok": True})

    # -----------------------------------------------------------------------
    # Launch
    # -----------------------------------------------------------------------

    def _run():
        app.run(host="0.0.0.0", port=WEB_PORT,
                debug=False, use_reloader=False, threaded=True)

    t = threading.Thread(target=_run, daemon=True, name="web-ui")
    t.start()
    log.info("Web UI: http://0.0.0.0:%d  (also http://<pi-ip>:%d)", WEB_PORT, WEB_PORT)


# ---------------------------------------------------------------------------
# Embedded single-page UI
# ---------------------------------------------------------------------------

HTML_UI = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>MIDI Hub</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{background:#0d0d1a;color:#e2e2f0;font-family:system-ui,sans-serif;
     height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{display:flex;justify-content:space-between;align-items:center;
       padding:10px 14px;background:#131327;border-bottom:1px solid #252540;flex-shrink:0}
header h1{font-size:1rem;font-weight:700;letter-spacing:.05em;color:#c8c8ff}
.mbtn{padding:7px 16px;border-radius:20px;border:none;cursor:pointer;
      font-size:.85rem;font-weight:600}
.mplay{background:#166534;color:#fff}
.mpause{background:#92400e;color:#fff}
.tabs{display:flex;background:#0d0d1a;border-bottom:1px solid #252540;flex-shrink:0}
.tab{flex:1;padding:9px 4px;text-align:center;cursor:pointer;font-size:.8rem;
     border-bottom:2px solid transparent;color:#555}
.tab.on{border-bottom-color:#818cf8;color:#818cf8}
.panel{display:none;flex:1;overflow-y:auto;flex-direction:column}
.panel.on{display:flex}
/* grid */
#gp{padding:10px}
.clabels{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin-bottom:5px}
.clabel{text-align:center;font-size:.62rem;color:#444;font-weight:700;text-transform:uppercase}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:5px}
.cell{aspect-ratio:1;border-radius:8px;cursor:pointer;position:relative;
      display:flex;align-items:center;justify-content:center;
      font-size:.72rem;font-weight:700;color:rgba(255,255,255,.65);
      border:2px solid transparent;user-select:none;transition:transform .08s}
.cell:active{transform:scale(.88)}
.cell.sel{border-color:#fff!important}
.crow{position:absolute;top:3px;left:5px;font-size:.48rem;color:rgba(255,255,255,.28)}
.se{background:#171728}
.sar{background:#7f1d1d;animation:br .45s infinite alternate}
.srec{background:#dc2626}
.sas{background:#14532d;animation:bg .45s infinite alternate}
.spl{background:#166534}
.spa{background:#78350f}
.smp{background:#1e3a5f;animation:bb .65s infinite alternate}
@keyframes br{to{background:#dc2626}}
@keyframes bg{to{background:#16a34a}}
@keyframes bb{to{background:#1d4ed8}}
/* action panel */
#ap{background:#131327;border-top:1px solid #252540;padding:11px 13px;flex-shrink:0;min-height:88px}
.apt{font-size:.72rem;color:#444;margin-bottom:7px}
.brow{display:flex;gap:7px;flex-wrap:wrap}
.btn{padding:7px 13px;border-radius:7px;border:none;cursor:pointer;font-size:.8rem;font-weight:600}
.bg{background:#166534;color:#fff}.br{background:#991b1b;color:#fff}
.ba{background:#92400e;color:#fff}.bd{background:#374151;color:#fff}.bb{background:#1e3a8a;color:#fff}
.srow{display:flex;gap:6px;margin-top:9px}
.srow input,.srow select{flex:1;padding:6px 9px;background:#0d0d1a;border:1px solid #252540;
  border-radius:6px;color:#e2e2f0;font-size:.8rem;min-width:0}
.srow select{flex:0 0 auto;width:78px}
/* banks / songs */
.bpanel{padding:12px 13px}
.bsec{margin-bottom:16px}
.bhdr{font-size:.8rem;font-weight:700;color:#818cf8;letter-spacing:.05em;
      text-transform:uppercase;margin-bottom:7px}
.li{display:flex;justify-content:space-between;align-items:center;
    padding:9px 11px;background:#131327;border-radius:8px;margin-bottom:4px;border:1px solid #252540}
.ln{font-size:.85rem;font-weight:500}
.lm{font-size:.68rem;color:#444;margin-top:2px}
.lbtns{display:flex;gap:6px}
.srow2{display:flex;gap:8px;margin-bottom:13px}
.srow2 input{flex:1;padding:8px 10px;background:#0d0d1a;border:1px solid #252540;
             border-radius:7px;color:#e2e2f0;font-size:.85rem}
/* upload */
.upanel{padding:14px}
.uzone{border:2px dashed #252540;border-radius:12px;padding:38px 20px;text-align:center;
       cursor:pointer;margin-bottom:13px}
.uzone.drag{border-color:#818cf8}
.uico{font-size:2.2rem;margin-bottom:8px}
.uhint{color:#444;font-size:.78rem;margin-top:4px}
#ust{font-size:.84rem;line-height:1.5}
/* modal */
.ov{position:fixed;inset:0;background:rgba(0,0,0,.78);display:flex;
    align-items:flex-end;z-index:200}
.mo{background:#131327;border-radius:16px 16px 0 0;padding:17px 13px;
    width:100%;max-height:80vh;overflow-y:auto}
.mo h3{margin-bottom:5px;font-size:.93rem}
.mhint{font-size:.72rem;color:#444;margin-bottom:11px}
.sgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.scell{padding:10px 3px;text-align:center;border-radius:7px;cursor:pointer;
       font-size:.63rem;border:1px solid #252540}
.scell:active{transform:scale(.9)}
/* toast */
#toast{position:fixed;bottom:64px;left:50%;transform:translateX(-50%);
       padding:8px 17px;border-radius:20px;font-size:.79rem;z-index:300;
       opacity:0;transition:opacity .22s;pointer-events:none;white-space:nowrap}
.empty{text-align:center;color:#333;padding:36px 16px;font-size:.82rem;line-height:1.6}
</style>
</head>
<body>
<header>
  <h1>MIDI Hub</h1>
  <button id="mbtn" class="mbtn mplay" onclick="toggleMaster()">&#9654; Play</button>
</header>
<nav class="tabs">
  <div class="tab on"  onclick="go('grid')">Grid</div>
  <div class="tab"     onclick="go('banks')">Loops</div>
  <div class="tab"     onclick="go('songs')">Songs</div>
  <div class="tab"     onclick="go('upload')">Upload</div>
</nav>

<div id="panel-grid" class="panel on">
  <div id="gp">
    <div class="clabels" id="clabels"></div>
    <div class="grid" id="grid"></div>
  </div>
  <div id="ap">
    <div class="apt" id="apt">Tap a clip</div>
    <div class="brow" id="abtns"></div>
    <div id="asave"></div>
  </div>
</div>

<div id="panel-banks" class="panel">
  <div class="bpanel" id="banks-root"><div class="empty">Loading&hellip;</div></div>
</div>

<div id="panel-songs" class="panel">
  <div style="padding:13px">
    <div class="srow2">
      <input id="sni" placeholder="Song name&hellip;" type="text">
      <button class="btn bb" onclick="saveSong()">Save Grid</button>
    </div>
    <div id="songs-root"><div class="empty">Loading&hellip;</div></div>
  </div>
</div>

<div id="panel-upload" class="panel">
  <div class="upanel">
    <div class="uzone" id="dz" onclick="document.getElementById('fi').click()">
      <div class="uico">&#127925;</div>
      <div>Tap to choose a .mid file</div>
      <div class="uhint">or drag &amp; drop</div>
    </div>
    <input type="file" id="fi" accept=".mid,.midi" style="display:none" onchange="doUpload(this)">
    <div id="ust"></div>
  </div>
</div>

<div id="modal" style="display:none" class="ov" onclick="closeModal()">
  <div class="mo" onclick="event.stopPropagation()">
    <h3>Load into slot</h3>
    <p class="mhint">Tap an empty or paused slot</p>
    <div class="sgrid" id="sgrid"></div>
    <button class="btn bd" style="width:100%;margin-top:11px" onclick="closeModal()">Cancel</button>
  </div>
</div>

<div id="toast"></div>

<script>
var S={clips:[],master_paused:false,col_names:['T8Dr','T8Ba','UNO','E-4']};
var sel=null, pend=null, tab='grid', pid=null;

var ICON={EMPTY:'',ARMED_REC:'REC',RECORDING:'REC',ARMED_STOP:'STP',
          PLAYING:'',PAUSED:'II',MASTER_PAUSED:''};
var CLS={EMPTY:'se',ARMED_REC:'sar',RECORDING:'srec',ARMED_STOP:'sas',
         PLAYING:'spl',PAUSED:'spa',MASTER_PAUSED:'smp'};

function startPoll(){if(pid)return;tick();pid=setInterval(tick,620);}
async function tick(){
  try{var r=await fetch('/api/state');if(!r.ok)return;S=await r.json();
    renderGrid();updateMbtn();if(sel)renderAP();}catch(e){}
}

function renderGrid(){
  var lb=document.getElementById('clabels');
  if(!lb.children.length) S.col_names.forEach(function(n){
    var d=document.createElement('div');d.className='clabel';d.textContent=n;lb.appendChild(d);
  });
  var g=document.getElementById('grid');
  S.clips.forEach(function(c){
    var id='c'+c.row+'_'+c.col, el=document.getElementById(id);
    if(!el){
      el=document.createElement('div');el.id=id;
      var rn=document.createElement('span');rn.className='crow';rn.textContent='R'+(c.row+1);
      var ic=document.createElement('span');ic.className='ci';
      el.appendChild(rn);el.appendChild(ic);
      el.addEventListener('click',(function(r,c){return function(){pick(r,c);};})(c.row,c.col));
      g.appendChild(el);
    }
    var isSel=sel&&sel.r===c.row&&sel.c===c.col;
    el.className='cell '+(CLS[c.state]||'se')+(isSel?' sel':'');
    el.querySelector('.ci').textContent=ICON[c.state]||'';
  });
}

function getC(r,c){return S.clips.find(function(x){return x.row===r&&x.col===c;});}

function pick(r,c){sel={r:r,c:c};renderGrid();renderAP();}

function renderAP(){
  if(!sel)return;
  var clip=getC(sel.r,sel.c);if(!clip)return;
  var bars=Math.round(clip.loop_ticks/96*10)/10;
  var info=clip.loop_ticks?' \u00b7 '+bars+'bar':'';
  document.getElementById('apt').textContent='R'+(clip.row+1)+' \u00b7 '+S.col_names[clip.col]+' \u00b7 '+clip.state+info;
  var btns=document.getElementById('abtns');btns.innerHTML='';
  document.getElementById('asave').innerHTML='';
  function b(txt,cls,fn){var el=document.createElement('button');
    el.className='btn '+cls;el.textContent=txt;el.onclick=fn;btns.appendChild(el);}
  var st=clip.state;
  if(st==='EMPTY')             b('Arm Rec','br',function(){ca('press');});
  else if(st==='ARMED_REC')    b('Cancel','bd',function(){ca('press');});
  else if(st==='RECORDING')    b('Stop','ba',function(){ca('press');});
  else if(st==='ARMED_STOP')   b('Keep Rec','bd',function(){ca('press');});
  else{
    b(st==='PLAYING'?'Pause':'Play',st==='PLAYING'?'ba':'bg',function(){ca('press');});
    b('Clear','br',function(){ca('clear');});
    var cn=(S.col_names[clip.col]||'clip').replace(/[^a-z0-9]/gi,'_');
    document.getElementById('asave').innerHTML=
      '<div class="srow"><input id="sn" type="text" placeholder="Name\u2026" value="r'+(clip.row+1)+'_'+cn+'">'+
      '<select id="sb"><option value="loops">Loops</option><option value="songs">Songs</option></select>'+
      '<button class="btn bb" onclick="saveClip()">Save</button></div>';
  }
}

async function ca(action){
  if(!sel)return;
  await fetch('/api/clip/'+sel.r+'/'+sel.c,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({action:action})});
  await tick();
}

async function saveClip(){
  if(!sel)return;
  var n=(document.getElementById('sn')||{}).value||'clip';
  var bk=(document.getElementById('sb')||{}).value||'loops';
  var r=await fetch('/api/clip/'+sel.r+'/'+sel.c+'/save',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n.trim(),bank:bk})});
  var d=await r.json();
  if(d.ok){toast('Saved "'+n+'" \u2192 '+bk);if(tab==='banks')fetchBanks();}
  else toast(d.error,1);
}

function updateMbtn(){
  var b=document.getElementById('mbtn');
  if(S.master_paused){b.className='mbtn mplay';b.innerHTML='&#9654; Play';}
  else{b.className='mbtn mpause';b.innerHTML='&#9646;&#9646; Pause';}
}
async function toggleMaster(){
  await fetch('/api/master',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:S.master_paused?'play':'pause'})});
  await tick();
}

// --- banks ---
async function fetchBanks(){
  try{var r=await fetch('/api/banks');renderBanks(await r.json());}catch(e){}
}
function renderBanks(banks){
  var el=document.getElementById('banks-root');
  var ks=Object.keys(banks);
  if(!ks.length){el.innerHTML='<div class="empty">No loops saved yet.</div>';return;}
  el.innerHTML='';
  ks.forEach(function(bk){
    var clips=banks[bk],sec=document.createElement('div');sec.className='bsec';
    var h=document.createElement('div');h.className='bhdr';h.textContent=bk;sec.appendChild(h);
    if(!clips.length){var e=document.createElement('div');e.className='empty';
      e.style.padding='8px 0';e.textContent='Empty';sec.appendChild(e);}
    clips.forEach(function(c){
      var bars=Math.round((c.loop_ticks||0)/96*10)/10;
      var li=document.createElement('div');li.className='li';
      li.innerHTML='<div><div class="ln">'+esc(c.name)+'</div>'+
        '<div class="lm">'+(c.col_name||'')+' \u00b7 '+bars+' bar'+(bars!==1?'s':'')+
        ' \u00b7 '+(c.events_count||0)+' ev</div></div>'+
        '<div class="lbtns"><button class="btn bg">Load</button><button class="btn br">&#10005;</button></div>';
      li.querySelectorAll('button')[0].onclick=function(){openPicker(bk,c.name);};
      li.querySelectorAll('button')[1].onclick=function(e){delClip(bk,c.name,e.target);};
      sec.appendChild(li);
    });
    el.appendChild(sec);
  });
}
function openPicker(bk,name){
  pend={bk:bk,name:name};
  var sg=document.getElementById('sgrid');sg.innerHTML='';
  for(var r=0;r<8;r++)for(var c=0;c<4;c++){
    var clip=getC(r,c),sc=CLS[(clip&&clip.state)||'EMPTY']||'se';
    var el=document.createElement('div');el.className='scell '+sc;
    el.innerHTML='<div style="font-weight:700">'+S.col_names[c]+'</div><div>R'+(r+1)+'</div>';
    el.addEventListener('click',(function(rr,cc){return function(){loadSlot(rr,cc);};})(r,c));
    sg.appendChild(el);
  }
  document.getElementById('modal').style.display='flex';
}
async function loadSlot(r,c){
  if(!pend)return;closeModal();
  var bk=pend.bk,name=pend.name;pend=null;
  var res=await fetch('/api/banks/'+encodeURIComponent(bk)+'/'+encodeURIComponent(name)+'/load',
    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({row:r,col:c})});
  var d=await res.json();
  if(d.ok)toast('Loaded "'+name+'" \u2192 R'+(r+1)+' '+S.col_names[c]);
  else toast(d.error,1);
  await tick();
}
async function delClip(bk,name,btn){
  if(!confirm('Delete "'+name+'" from '+bk+'?'))return;
  btn.disabled=true;
  var d=await(await fetch('/api/banks/'+encodeURIComponent(bk)+'/'+encodeURIComponent(name),{method:'DELETE'})).json();
  if(d.ok){toast('Deleted "'+name+'"');fetchBanks();}else{toast(d.error,1);btn.disabled=false;}
}
function closeModal(){document.getElementById('modal').style.display='none';pend=null;}

// --- songs ---
async function fetchSongs(){
  try{var r=await fetch('/api/songs');renderSongs(await r.json());}catch(e){}
}
function renderSongs(songs){
  var el=document.getElementById('songs-root');
  if(!songs.length){el.innerHTML='<div class="empty">No songs yet.<br>Record loops, then tap Save Grid.</div>';return;}
  el.innerHTML='';
  songs.forEach(function(s){
    var li=document.createElement('div');li.className='li';
    li.innerHTML='<div><div class="ln">'+esc(s.name)+'</div>'+
      '<div class="lm">'+s.clips_count+' clip'+(s.clips_count!==1?'s':'')+'</div></div>'+
      '<div class="lbtns"><button class="btn bg">Load</button><button class="btn br">&#10005;</button></div>';
    li.querySelectorAll('button')[0].onclick=function(){loadSong(s.name);};
    li.querySelectorAll('button')[1].onclick=function(e){delSong(s.name,e.target);};
    el.appendChild(li);
  });
}
async function saveSong(){
  var name=document.getElementById('sni').value.trim();
  if(!name){toast('Enter a name first',1);return;}
  var d=await(await fetch('/api/songs',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:name})})).json();
  if(d.ok){toast('Song "'+name+'" saved ('+d.clips_count+' clips)');fetchSongs();}
  else toast(d.error,1);
}
async function loadSong(name){
  if(!confirm('Load "'+name+'"? Clears the current grid.'))return;
  var d=await(await fetch('/api/songs/'+encodeURIComponent(name)+'/load',
    {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
  if(d.ok){toast('Loaded "'+name+'"');await tick();}else toast(d.error,1);
}
async function delSong(name,btn){
  if(!confirm('Delete song "'+name+'"?'))return;
  btn.disabled=true;
  var d=await(await fetch('/api/songs/'+encodeURIComponent(name),{method:'DELETE'})).json();
  if(d.ok){toast('Deleted');fetchSongs();}else{toast(d.error,1);btn.disabled=false;}
}

// --- upload ---
async function doUpload(inp){
  var file=inp.files[0];if(!file)return;
  var st=document.getElementById('ust');
  st.style.color='#555';st.textContent='Uploading '+file.name+'\u2026';
  var form=new FormData();form.append('file',file);
  try{
    var d=await(await fetch('/api/upload',{method:'POST',body:form})).json();
    if(d.ok){
      var bars=Math.round(d.loop_ticks/96*10)/10;
      st.style.color='#4ade80';
      st.textContent='\u2713 Imported "'+d.name+'" \u2014 '+d.events_count+' events, '+bars+' bars';
      if(tab==='banks')fetchBanks();
    }else{st.style.color='#dc2626';st.textContent='\u2717 '+d.error;}
  }catch(e){st.style.color='#dc2626';st.textContent='\u2717 '+e.message;}
  inp.value='';
}
document.addEventListener('DOMContentLoaded',function(){
  var z=document.getElementById('dz');
  z.addEventListener('dragover',function(e){e.preventDefault();z.classList.add('drag');});
  z.addEventListener('dragleave',function(){z.classList.remove('drag');});
  z.addEventListener('drop',function(e){
    e.preventDefault();z.classList.remove('drag');
    var f=e.dataTransfer.files[0];if(!f)return;
    var i=document.getElementById('fi');
    try{var dt=new DataTransfer();dt.items.add(f);i.files=dt.files;}catch(x){return;}
    doUpload(i);
  });
});

// --- tabs ---
var TABS=['grid','banks','songs','upload'];
function go(t){
  tab=t;
  document.querySelectorAll('.tab').forEach(function(el,i){el.classList.toggle('on',TABS[i]===t);});
  document.querySelectorAll('.panel').forEach(function(el){el.classList.remove('on');});
  document.getElementById('panel-'+t).classList.add('on');
  if(t==='banks')fetchBanks();
  if(t==='songs')fetchSongs();
}

// --- utils ---
function toast(msg,err){
  var t=document.getElementById('toast');
  t.textContent=msg;t.style.background=err?'#991b1b':'#166534';
  t.style.opacity='1';clearTimeout(t._t);
  t._t=setTimeout(function(){t.style.opacity='0';},2800);
}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

startPoll();
</script>
</body>
</html>
"""

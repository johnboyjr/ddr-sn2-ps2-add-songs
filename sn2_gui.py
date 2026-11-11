"""SuperNOVA 2 Song Swap: a small local app around sn2.py.

Pick the original SuperNOVA 2 (US) .iso, add StepMania songs, choose which
of the 72 songs each one replaces, then save a patched copy. Runs only on
this computer; the page talks to 127.0.0.1.
"""
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sn2  # noqa: E402
import sm2ddr  # noqa: E402

PORT = 8766
TOKEN = secrets.token_hex(16)
CONFIG = HERE / "sn2_gui.json"
lock = threading.Lock()
keep = []  # temporary folders for extracted zips
state = {"iso": "", "songs": [], "slots": [], "next": 1, "busy": False,
         "log": [], "error": "", "result": "", "rev": 0, "stop": False}


def load_config():
    try:
        return json.loads(CONFIG.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(**values):
    cfg = load_config()
    cfg.update(values)
    try:
        CONFIG.write_text(json.dumps(cfg, indent=1), "utf-8")
    except OSError:
        pass


def bump():
    state["rev"] += 1


def log(msg):
    with lock:
        state["log"].append(str(msg))
        state["log"] = state["log"][-300:]
        bump()


# ------------------------------------------------------------ dialogs
def powershell(script):
    """Run a Windows Forms dialog; returns its printed lines, or None."""
    if os.name != "nt":
        return None
    full = ("Add-Type -AssemblyName System.Windows.Forms;"
            "$o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true};"
            + script)
    try:
        run = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", full],
            capture_output=True, text=True, timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return [ln.strip() for ln in run.stdout.splitlines() if ln.strip()]


def ps_quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def pick_iso(start):
    folder = str(Path(start).parent) if start else str(Path.home() / "Downloads")
    return powershell(
        "$d=New-Object System.Windows.Forms.OpenFileDialog;"
        "$d.Title='Choose the original SuperNOVA 2 disc image';"
        "$d.Filter='Disc image (*.iso)|*.iso|All files (*.*)|*.*';"
        f"$d.InitialDirectory={ps_quote(folder)};"
        "if($d.ShowDialog($o) -eq 'OK'){$d.FileName}")


def pick_songs():
    return powershell(
        "$d=New-Object System.Windows.Forms.OpenFileDialog;"
        "$d.Title='Choose StepMania songs (zips or .sm/.ssc files)';"
        "$d.Filter='StepMania songs (*.zip;*.sm;*.ssc)|*.zip;*.sm;*.ssc|All files (*.*)|*.*';"
        "$d.Multiselect=$true;"
        f"$d.InitialDirectory={ps_quote(Path.home() / 'Downloads')};"
        "if($d.ShowDialog($o) -eq 'OK'){$d.FileNames}")


def pick_folder():
    return powershell(
        "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description='Choose a folder of StepMania songs (a pack)';"
        "if($d.ShowDialog($o) -eq 'OK'){$d.SelectedPath}")


def pick_save(start):
    folder = str(Path(start).parent) if start else str(Path.home() / "Downloads")
    return powershell(
        "$d=New-Object System.Windows.Forms.SaveFileDialog;"
        "$d.Title='Save the patched disc image as';"
        "$d.Filter='Disc image (*.iso)|*.iso';"
        "$d.FileName='DDR SuperNOVA 2 custom.iso';"
        f"$d.InitialDirectory={ps_quote(folder)};"
        "$d.OverwritePrompt=$true;"
        "if($d.ShowDialog($o) -eq 'OK'){$d.FileName}")


# ------------------------------------------------------------ actions
def set_iso(path):
    path = str(path).strip().strip('"')
    game = sn2.Game(path)
    if not game.iso:
        raise sn2.SN2Error("Choose the .iso file itself, not a folder.")
    rows = game.songs()
    with lock:
        state["iso"] = path
        state["slots"] = [dict(number=n, code=c, title=t,
                               shop=n > 40) for n, c, t in rows]
        state["error"] = ""
        bump()
    save_config(iso=path)


def describe(simfile, source):
    song = sm2ddr.parse_simfile(simfile)
    charts = []
    for c in song.charts:
        if c.style in ("dance-single", "dance-double"):
            style = "SP" if c.style == "dance-single" else "DP"
            name = {"beginner": "Beginner", "easy": "Basic", "medium": "Difficult",
                    "hard": "Expert", "challenge": "Challenge"}.get(c.difficulty)
            if name:
                charts.append(f"{style} {name} {c.meter}")
    bpms = [b for _, b in song.bpms]
    bpm = f"{round(min(bpms))}" if bpms and min(bpms) == max(bpms) else \
        (f"{round(min(bpms))}-{round(max(bpms))}" if bpms else "")
    notes, error = [], "" if charts else "No single or double charts"
    if charts:
        try:
            sn2.ps2_chart(song, notes)
        except (sn2.SN2Error, sm2ddr.ConvertError) as e:
            error = str(e)
    has_challenge = any(" Challenge " in c for c in charts)
    return dict(title=song.title or simfile.stem, artist=song.artist, bpm=bpm,
                charts=charts, simfile=str(simfile), source=str(source),
                slot="", error=error, notes=notes,
                hasChallenge=has_challenge, challenge=has_challenge)


def add_paths(paths):
    added, problems = 0, []
    for raw in paths:
        p = Path(str(raw).strip().strip('"'))
        try:
            if p.is_dir():
                simfiles = sn2.find_simfiles(p)
                if not simfiles:
                    raise sn2.SN2Error("no .sm or .ssc files inside")
            elif p.suffix.lower() == ".zip":
                simfile = sn2.open_song(p, keep)
                simfiles = sn2.find_simfiles(keep[-1].name) or [simfile]
            else:
                simfiles = [sn2.open_song(p, keep)]
            for sf in simfiles:
                try:
                    item = describe(sf, p)
                except Exception as e:  # noqa: BLE001 - shown to the user
                    item = dict(title=sf.parent.name, artist="", bpm="",
                                charts=[], simfile=str(sf), source=str(p),
                                slot="", error=str(e))
                with lock:
                    item["id"] = state["next"]
                    state["next"] += 1
                    state["songs"].append(item)
                    bump()
                added += 1
        except Exception as e:  # noqa: BLE001
            problems.append(f"{p.name}: {e}")
    return added, problems


def auto_assign():
    """Give each song without a slot the next free one, starting at song 1."""
    with lock:
        used = {s["slot"] for s in state["songs"] if s["slot"]}
        free = [x["code"] for x in state["slots"] if x["code"] not in used]
        for s in state["songs"]:
            if not s["slot"] and not s["error"] and free:
                s["slot"] = free.pop(0)
        bump()


def run_patch(out):
    items = [(s["simfile"], s["slot"], bool(s.get("challenge")))
             for s in state["songs"] if s["slot"]]
    try:
        sn2.replace_many(state["iso"], items, out, progress=log,
                         cancelled=lambda: state["stop"])
        with lock:
            state["result"] = out
            bump()
    except Exception as e:  # noqa: BLE001
        log(f"Error: {e}")
        if not isinstance(e, (sn2.SN2Error, sm2ddr.ConvertError)):
            log(traceback.format_exc(limit=3))
        with lock:
            state["error"] = str(e)
            bump()
    finally:
        with lock:
            state["busy"] = False
            state["stop"] = False
            bump()


# ------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            page = PAGE.replace("__TOKEN__", TOKEN)
            return self.send(200, page.encode(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            if self.headers.get("X-Token") != TOKEN:
                return self.send(403, {"error": "bad token"})
            with lock:
                return self.send(200, state)
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.headers.get("X-Token") != TOKEN:
            return self.send(403, {"error": "bad token"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        try:
            result = self.route(self.path, body)
            self.send(200, result or {"ok": True})
        except (sn2.SN2Error, sm2ddr.ConvertError, OSError, ValueError) as e:
            self.send(400, {"error": str(e)})

    def route(self, path, body):
        if state["busy"] and path not in ("/api/stop",):
            raise sn2.SN2Error("Please wait until the current job finishes.")
        if path == "/api/iso":
            if body.get("browse"):
                got = pick_iso(state["iso"])
                if got is None:
                    return {"dialog": False}
                if not got:
                    return {"cancelled": True}
                set_iso(got[0])
            else:
                set_iso(body.get("path", ""))
            return {}
        if path == "/api/add":
            if body.get("browse") in ("files", "folder"):
                got = pick_songs() if body["browse"] == "files" else pick_folder()
                if got is None:
                    return {"dialog": False}
                paths = got
            else:
                paths = [body.get("path", "")]
            if not paths:
                return {"cancelled": True}
            added, problems = add_paths(paths)
            return {"added": added, "problems": problems}
        if path == "/api/slot":
            with lock:
                song = next(s for s in state["songs"] if s["id"] == body["id"])
                slot = body.get("slot", "")
                if slot:
                    for other in state["songs"]:
                        if other is not song and other["slot"] == slot:
                            other["slot"] = ""
                song["slot"] = slot
                bump()
            return {}
        if path == "/api/challenge":
            with lock:
                song = next(s for s in state["songs"] if s["id"] == body["id"])
                song["challenge"] = bool(body.get("on")) and song.get("hasChallenge", False)
                bump()
            return {}
        if path == "/api/remove":
            with lock:
                state["songs"] = [s for s in state["songs"] if s["id"] != body["id"]]
                bump()
            return {}
        if path == "/api/clear":
            with lock:
                state["songs"] = []
                bump()
            return {}
        if path == "/api/auto":
            auto_assign()
            return {}
        if path == "/api/patch":
            if not state["iso"]:
                raise sn2.SN2Error("Choose the original SuperNOVA 2 .iso first.")
            if not any(s["slot"] for s in state["songs"]):
                raise sn2.SN2Error("Pick a song to replace for at least one import.")
            out = body.get("path", "").strip().strip('"')
            if body.get("browse"):
                got = pick_save(load_config().get("out", state["iso"]))
                if got is None:
                    return {"dialog": False}
                if not got:
                    return {"cancelled": True}
                out = got[0]
            if not out:
                raise sn2.SN2Error("Choose where to save the new .iso.")
            if not out.lower().endswith(".iso"):
                out += ".iso"
            if Path(out).resolve() == Path(state["iso"]).resolve():
                raise sn2.SN2Error("Save to a different file; the original is kept.")
            free = shutil.disk_usage(Path(out).parent).free
            need = Path(state["iso"]).stat().st_size + 10 * 2 ** 20 * \
                sum(1 for s in state["songs"] if s["slot"])
            if free < need:
                raise sn2.SN2Error(f"Not enough free space there: about "
                                   f"{need / 2**30:.1f} GB is needed.")
            save_config(out=out)
            with lock:
                state.update(busy=True, log=[], error="", result="", stop=False)
                bump()
            threading.Thread(target=run_patch, args=(out,), daemon=True).start()
            return {}
        if path == "/api/stop":
            state["stop"] = True
            return {}
        raise sn2.SN2Error("unknown request")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SuperNOVA 2 Song Swap</title>
<style>
:root{--bg:#0e1117;--panel:#171b24;--line:#2a3140;--text:#e8ebf2;--muted:#9aa3b5;
--accent:#ff4fa3;--ok:#5fd38a;--bad:#ff6b6b;--warn:#f5c050;--field:#0b0e14}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 "Segoe UI",system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:28px 16px 60px}
h1{margin:0 0 4px;font-size:26px}h1 span{color:var(--accent)}
.sub{color:var(--muted);margin:0 0 22px}
section{background:var(--panel);border:1px solid var(--line);border-radius:14px;
padding:18px;margin-bottom:16px}
h2{margin:0 0 12px;font-size:16px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.chk{display:flex;align-items:center;gap:4px;white-space:nowrap;font-size:14px}
input,select{background:var(--field);color:var(--text);border:1px solid var(--line);
border-radius:9px;padding:10px 12px;font:inherit;min-width:0}
input{flex:1}
button{background:#262d3b;color:var(--text);border:1px solid var(--line);
border-radius:9px;padding:10px 14px;font:inherit;font-weight:600;cursor:pointer}
button:hover:not(:disabled){border-color:var(--accent)}
button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--accent);border-color:var(--accent);color:#16070f;
width:100%;padding:13px;font-size:16px}
button.link{background:none;border:none;color:var(--muted);padding:6px}
.help{color:var(--muted);font-size:13px;margin:8px 0 0}
.status{font-size:13px;margin-top:8px}.ok{color:var(--ok)}.bad{color:var(--bad)}
.song{display:grid;grid-template-columns:1fr auto minmax(240px,340px) auto;gap:12px;
align-items:center;padding:12px 0;border-top:1px solid var(--line)}
.song:first-child{border-top:none}
.title{font-weight:600}.meta{color:var(--muted);font-size:13px}
.err{color:var(--bad);font-size:13px}.warn{color:var(--warn);font-size:13px}
.count{color:var(--muted);font-weight:400}
pre{background:var(--field);border:1px solid var(--line);border-radius:9px;
padding:10px;max-height:220px;overflow:auto;font-size:12px;white-space:pre-wrap;margin:12px 0 0}
.empty{color:var(--muted);padding:8px 0}
@media (max-width:700px){.song{grid-template-columns:1fr}}
</style></head><body><main>
<h1>SuperNOVA 2 <span>Song Swap</span></h1>
<p class="sub">Replace songs in DDR SuperNOVA 2 (US) with StepMania songs.
Your original disc image is never changed.</p>

<section><h2>1. Original disc image</h2>
<div class="row"><input id="iso" aria-label="Disc image" placeholder="Path to Dance Dance Revolution SuperNOVA 2.iso">
<button id="isoUse">Use</button><button id="isoBrowse">Browse…</button></div>
<div class="status" id="isoStatus"></div></section>

<section><h2>2. Add StepMania songs <span class="count" id="songCount"></span></h2>
<div class="row"><button id="addFiles">Add songs…</button>
<button id="addFolder">Add a pack folder…</button>
<input id="addPath" aria-label="Song path" placeholder="…or paste a zip, song folder or pack folder">
<button id="addUse">Add</button></div>
<p class="help">Zips, song folders, .sm or .ssc files. A pack folder adds every song in it.</p>
<div class="status" id="addStatus"></div></section>

<section><h2>3. Choose what each song replaces</h2>
<div class="row" style="margin-bottom:6px"><button id="auto">Fill empty choices in order</button>
<button id="clear" class="link">Remove all</button></div>
<div id="songs"></div></section>

<section><h2>4. Patch and save</h2>
<button class="primary" id="patch">Patch and save…</button>
<div class="row" style="margin-top:10px" id="savePathRow" hidden>
<input id="savePath" aria-label="Save as" placeholder="Where to save the new .iso">
<button id="saveUse">Save here</button></div>
<div class="status" id="patchStatus"></div>
<pre id="log" hidden></pre></section>
</main>
<script>
const T='__TOKEN__';const $=id=>document.getElementById(id);
let S=null,rev=-1,focused=false;
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',
 headers:{'X-Token':T,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
 const j=await r.json();if(!r.ok)throw new Error(j.error||'failed');return j}
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;
 if(text!=null)e.textContent=text;return e}
function status(id,text,ok){const s=$(id);s.textContent=text||'';s.className='status '+(ok?'ok':'bad')}
async function act(fn,where){try{const r=await fn();if(r&&r.dialog===false&&where)
 status(where,'The file dialog is not available here; paste the path instead.',false);
 return r}catch(e){if(where)status(where,e.message,false)}finally{refresh(true)}}
function render(){
 const busy=S.busy;
 if(!focused&&!$('iso').value&&S.iso)$('iso').value=S.iso;
 status('isoStatus',S.iso?`✓ ${S.slots.length} songs found in ${S.iso.split(/[\\/]/).pop()}`:'',true);
 $('songCount').textContent=S.songs.length?`(${S.songs.length})`:'';
 const box=$('songs');box.replaceChildren();
 if(!S.songs.length)box.append(el('div','empty','No songs added yet.'));
 const used={};S.songs.forEach(s=>{if(s.slot)used[s.slot]=s.title});
 for(const s of S.songs){
  const row=el('div','song'),info=el('div');
  info.append(el('div','title',s.title),
   el('div','meta',[s.artist,s.bpm&&s.bpm+' BPM'].filter(Boolean).join(' · ')),
   el('div','meta',s.charts.join(', ')));
  if(s.error)info.append(el('div','err',s.error));
  for(const n of (s.notes||[]))info.append(el('div','warn',n));
  const sel=el('select');sel.setAttribute('aria-label','Song to replace for '+s.title);
  sel.disabled=busy||!!s.error||!S.slots.length;
  sel.append(new Option(S.slots.length?'— choose a song to replace —':'Choose the disc image first',''));
  const groups=[['Available from the start',x=>!x.shop],['Shop songs',x=>x.shop]];
  for(const [label,f] of groups){const g=el('optgroup');g.label=label;
   for(const x of S.slots.filter(f)){
    const taken=used[x.code]&&s.slot!==x.code;
    g.append(new Option(`${x.number}. ${x.title}`+(taken?`  (used by ${used[x.code]})`:''),x.code))}
   sel.append(g)}
  sel.value=s.slot||'';
  sel.onfocus=()=>focused=true;sel.onblur=()=>focused=false;
  sel.onchange=()=>{focused=false;act(()=>api('/api/slot',{id:s.id,slot:sel.value}))};
  const rm=el('button','link','Remove');rm.disabled=busy;
  rm.onclick=()=>act(()=>api('/api/remove',{id:s.id}));
  // Challenge: on for songs with a Challenge chart; turns Challenge on in the game for this song
  const ch=el('label','chk');
  const cb=el('input');cb.type='checkbox';cb.checked=!!s.challenge;cb.disabled=busy||!s.hasChallenge;
  cb.onchange=()=>act(()=>api('/api/challenge',{id:s.id,on:cb.checked}));
  ch.title=s.hasChallenge?'Let this song be played on Challenge':'This song has no Challenge chart';
  ch.append(cb,document.createTextNode(' Challenge'));
  if(!s.hasChallenge)ch.style.opacity='0.45';
  row.append(info,ch,sel,rm);box.append(row)}
 const n=S.songs.filter(s=>s.slot).length;
 $('patch').textContent=busy?'Working…':`Patch and save${n?` (${n} song${n>1?'s':''})`:''}…`;
 $('patch').disabled=busy||!n||!S.iso;
 for(const id of ['isoUse','isoBrowse','addFiles','addFolder','addUse','auto','clear'])$(id).disabled=busy;
 const lg=$('log');lg.hidden=!S.log.length;lg.textContent=S.log.join('\n');lg.scrollTop=lg.scrollHeight;
 if(S.result)status('patchStatus','✓ Saved '+S.result,true);
 else if(S.error)status('patchStatus',S.error,false);
 else if(!busy)status('patchStatus','',true);
}
async function refresh(force){try{const s=await api('/api/state');
 if(force||s.rev!==rev){S=s;rev=s.rev;if(!focused)render()}}catch(e){}}
$('isoUse').onclick=()=>act(()=>api('/api/iso',{path:$('iso').value}),'isoStatus');
$('isoBrowse').onclick=()=>act(()=>api('/api/iso',{browse:true}),'isoStatus');
$('iso').onfocus=()=>focused=true;$('iso').onblur=()=>focused=false;
async function added(p){const r=await act(p,'addStatus');if(r&&r.added!=null)
 status('addStatus',`Added ${r.added} song${r.added==1?'':'s'}.`+(r.problems.length?' Problems: '+r.problems.join('; '):''),!r.problems.length)}
$('addFiles').onclick=()=>added(()=>api('/api/add',{browse:'files'}));
$('addFolder').onclick=()=>added(()=>api('/api/add',{browse:'folder'}));
$('addUse').onclick=()=>added(()=>api('/api/add',{path:$('addPath').value}));
$('auto').onclick=()=>act(()=>api('/api/auto',{}));
$('clear').onclick=()=>{if(confirm('Remove all added songs from the list?'))act(()=>api('/api/clear',{}))};
$('patch').onclick=async()=>{const r=await act(()=>api('/api/patch',{browse:true}),'patchStatus');
 if(r&&r.dialog===false){$('savePathRow').hidden=false;$('savePath').focus()}};
$('saveUse').onclick=()=>act(()=>api('/api/patch',{path:$('savePath').value}),'patchStatus');
refresh(true);setInterval(()=>refresh(false),800);
</script></body></html>"""


def main():
    sn2.find_ffmpeg()
    cfg = load_config()
    if cfg.get("iso") and Path(cfg["iso"]).exists():
        try:
            set_iso(cfg["iso"])
        except Exception:  # noqa: BLE001
            pass
    else:
        guess = Path.home() / "Downloads" / "Dance Dance Revolution SuperNOVA 2" / \
            "Dance Dance Revolution SuperNOVA 2.iso"
        if guess.exists():
            try:
                set_iso(guess)
            except Exception:  # noqa: BLE001
                pass
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"SuperNOVA 2 Song Swap is running at {url}")
    print("Keep this window open while you use it. Close it to quit.")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

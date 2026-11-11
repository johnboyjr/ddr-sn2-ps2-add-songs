#!/usr/bin/env python3
"""Song manager for sm2ddr. Opens in your browser, runs only on this computer."""
import contextlib
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import types
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
def find_ffmpeg():
    """Look for ffmpeg next to the app, then in nearby folders and Downloads."""
    if shutil.which("ffmpeg", path=str(HERE)) or shutil.which("ffmpeg"):
        return HERE
    roots = [HERE.parent, HERE.parent.parent, Path.home() / "Downloads",
             Path.home() / "Desktop", Path("C:/ffmpeg")]
    patterns = ["ffmpeg.exe", "*/ffmpeg.exe", "*/bin/ffmpeg.exe",
                "*/*/ffmpeg.exe", "*/*/bin/ffmpeg.exe"]
    for root in roots:
        for pattern in patterns:
            try:
                hit = next(root.glob(pattern), None)
            except OSError:
                hit = None
            if hit:
                return hit.parent
    return HERE


os.environ["PATH"] = os.pathsep.join(
    dict.fromkeys([str(HERE), str(find_ffmpeg()),
                   os.environ.get("PATH", "")]))
sys.path.insert(0, str(HERE))
import sm2ddr  # noqa: E402

PORT = 8765
TOKEN = secrets.token_hex(16)
CONFIG = HERE / "sm2ddr_gui.json"
DEFAULTS = {"game": "", "template": "hypd", "series": 20, "bemaniflag": 4,
            "folderNames": {}}
STAGE = Path(tempfile.mkdtemp(prefix="sm2ddr_"))
MAX_UPLOAD = 2 * 1024 ** 3

state = {"queue": [], "log": [], "busy": False, "next": 1, "rev": 0}
# Order comes from the version table in gamemdx.dll.
SERIES = {1: "1st", 2: "2ndMIX", 3: "3rdMIX", 4: "4thMIX", 5: "5thMIX",
          6: "MAX", 7: "MAX2", 8: "EXTREME", 9: "SuperNOVA", 10: "SuperNOVA2",
          11: "X", 12: "X2", 13: "X3", 14: "2013", 15: "2014", 16: "2015",
          17: "A", 18: "A20", 19: "A20 PLUS", 20: "CS VERSION", 21: "A3, needs the A3/WORLD patch",
          22: "WORLD, needs the A3/WORLD patch"}
# bemaniflag bit -> CS VERSION folder, confirmed from in-game video.
FOLDERS = {4: "DDR GRAND PRIX", 8: "DDR Mobile Games", 16: "DDR UNIVERSE",
           32: "DDR ULTRAMIX", 64: "DDR HOTTEST PARTY", 128: "PlayStation 2",
           256: "PlayStation", 1: "PLACEHOLDER (slot 1)", 2: "PLACEHOLDER (slot 2)",
           1024: "Unused slot 1024", 2048: "Unused slot 2048"}
lock = threading.Lock()


def load_config():
    try:
        return {**DEFAULTS, **json.loads(CONFIG.read_text("utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


config = load_config()


def ffmpeg_folder(text):
    """Return the folder holding ffmpeg for a picked file or folder."""
    path = Path(text.strip().strip('"'))
    for folder in (path, path / "bin", path.parent):
        if folder.is_dir() and shutil.which("ffmpeg", path=str(folder)):
            return folder
    return None


def use_ffmpeg(text):
    folder = ffmpeg_folder(text) if text else None
    if folder:
        os.environ["PATH"] = f"{folder}{os.pathsep}{os.environ['PATH']}"
    return folder


use_ffmpeg(config.get("ffmpeg", ""))


def save_config():
    CONFIG.write_text(json.dumps(config, indent=2), "utf-8")


def game_path():
    return Path(config["game"]) if config["game"] else None


def checks():
    game = game_path()
    try:
        import numpy  # noqa: F401
        numpy_ok = True
    except ImportError:
        numpy_ok = False
    found, songs, template_ok, problem = False, [], False, ""
    if game and (game / "data").is_dir():
        try:
            db = sm2ddr.load_musicdb(game)
            found = db is not None
            if not found:
                problem = "No song list found in this folder."
            else:
                template_ok = any(e.findtext("basename") == config["template"]
                                  for e in db[0])
                songs = [{"base": e.findtext("basename"),
                          "title": e.findtext("title"),
                          "artist": e.findtext("artist") or "",
                          "levels": e.findtext("diffLv")}
                         for e in sm2ddr.tracked_songs(game)]
        except Exception as e:  # a bad archive should not stop the page
            problem = str(e)
    elif game:
        problem = "This folder has no data folder inside it."
    a3 = "missing"
    if game and (game / "gamemdx.dll").exists():
        a3 = sm2ddr.a3_patch_state(game / "gamemdx.dll")
    return {"a3": a3, "game": config["game"], "gameOk": found, "problem": problem,
            "numpy": numpy_ok, "ffmpeg": bool(shutil.which("ffmpeg")),
            "templateOk": template_ok, "template": config["template"],
            "installed": songs, "world": config.get("world", ""), "isWorld": is_world(),
            "ffmpegPath": shutil.which("ffmpeg") or ""}


# DDR WORLD numbers its own versions: 20 is A3 and 21 is WORLD.
WORLD_SERIES = {**{n: SERIES[n] for n in range(1, 20)}, 20: "A3", 21: "WORLD"}


def is_world():
    """True when the game folder is a DDR WORLD install, not the omnimix."""
    game = game_path()
    return bool(game and (game / "data" / "arc" / "startup.arc").exists()
                and not (game / "data" / "arc" / "startup_eng").exists())


def series_table():
    return WORLD_SERIES if is_world() else SERIES


def series_name(number):
    label = series_table().get(number)
    if is_world():
        return f"DDR {label or number}"
    return f"Series {number}" + (f" ({label})" if label else "")


def folder_name(flag):
    named = config["folderNames"].get(str(flag))
    return named or FOLDERS.get(flag, f"Flag {flag}")


def library():
    """Every song in the game, plus the places songs can go."""
    game = game_path()
    db = sm2ddr.load_musicdb(game) if game else None
    world = is_world()
    numbers = list(series_table())
    if world and db:
        # Only the version folders this game actually has songs in.
        present = {int(e.findtext("series") or 0) for e in db[0]}
        numbers = [n for n in numbers if n in present]
    versions = [{"value": n, "name": series_name(n)} for n in numbers]
    if not db:
        return {"songs": [], "rev": state["rev"], "versions": versions,
                "folders": []}
    hidden = sm2ddr.hidden_songs(game)
    deleted = sm2ddr.deleted_songs(game)
    courses = sm2ddr.course_songs(game)
    custom = sm2ddr.tracked_songs(game)
    custom_names = {e.findtext("basename") for e in custom}
    songs, counts = [], {}
    for entry in list(db[0]) + custom:
        base = entry.findtext("basename")
        if base in deleted or (base in custom_names and entry not in custom):
            continue
        number = int(entry.findtext("series") or 0)
        flag = int(entry.findtext("bemaniflag") or 0)
        for bit in FOLDERS:
            if flag & bit:
                counts[bit] = counts.get(bit, 0) + 1
        levels = [int(x) for x in (entry.findtext("diffLv") or "").split()]
        single = [x for x in levels[:5] if x and x != 255]
        songs.append({
            "base": base, "title": entry.findtext("title") or base,
            "artist": entry.findtext("artist") or "", "series": number,
            "flag": flag, "category": series_name(number),
            "folder": folder_name(flag) if flag else "",
            "top": max(single) if single else 0,
            "custom": base in custom_names, "hidden": base in hidden,
            "course": entry.findtext("mcode") in courses})
    songs.sort(key=lambda s: s["title"].lower())
    folders = [] if world else [
        {"value": bit, "name": folder_name(bit), "count": counts.get(bit, 0),
         "named": str(bit) in config["folderNames"]} for bit in FOLDERS]
    return {"songs": songs, "rev": state["rev"], "versions": versions,
            "folders": folders}


def describe(path):
    """Summarize one simfile for the review list."""
    sm2ddr.warnings_seen.clear()
    with contextlib.redirect_stderr(open(os.devnull, "w")):
        try:
            song = sm2ddr.parse_simfile(path)
        except sm2ddr.ConvertError as e:
            return {"title": path.parent.name, "artist": "", "charts": [],
                    "bpm": "", "error": f"Can't convert: this song {e}."}
    bpms = [b for _, b in song.bpms]
    low, high = round(min(bpms)), round(max(bpms))
    names = {"beginner": "Beginner", "easy": "Basic", "medium": "Difficult",
             "hard": "Expert", "challenge": "Challenge"}
    charts, seen = [], set()
    notes = {"mines": 0, "rolls": 0}
    for c in song.charts:
        key = (c.style, c.difficulty)
        if c.style not in sm2ddr.STYLES or c.difficulty not in names \
                or key in seen or key == ("dance-double", "beginner"):
            continue
        seen.add(key)
        text = c.notes.upper()
        notes["mines"] += text.count("M")
        notes["rolls"] += text.count("4")
        charts.append(("SP " if c.style == "dance-single" else "DP ")
                      + f"{names[c.difficulty]} {c.meter}")
    error = "" if charts else "Can't convert: no single or double charts."
    title = song.title + (f" {song.subtitle}" if song.subtitle else "")
    return {"title": title, "artist": song.artist, "charts": charts,
            "bpm": str(high) if low == high else f"{low}-{high}",
            "error": error, **notes}


def add_to_queue(root):
    paths = sm2ddr.find_songs(root)
    added = 0
    for path in paths:
        item = describe(path)
        with lock:
            item.update(id=state["next"], folder=str(path.parent),
                        series=21 if is_world() else config["series"],
                        flag=0 if is_world() else config["bemaniflag"])
            state["next"] += 1
            state["queue"].append(item)
        added += 1
    return added


def extract_zip(source, target):
    with zipfile.ZipFile(source) as z:
        for info in z.infolist():
            dest = (target / info.filename).resolve()
            if not dest.is_relative_to(target.resolve()):
                raise sm2ddr.ConvertError("unsafe path inside zip")
        z.extractall(target)


class LogWriter:
    def write(self, text):
        for line in text.splitlines():
            if line.strip():
                with lock:
                    state["log"].append(line.rstrip())

    def flush(self):
        pass


def run_job(work):
    def runner():
        writer = LogWriter()
        try:
            with contextlib.redirect_stdout(writer), \
                    contextlib.redirect_stderr(writer):
                work()
        except SystemExit as e:
            writer.write(f"Stopped: {e}")
        except PermissionError:
            writer.write("Stopped: a game file is in use. Close the game and "
                         "try again.")
        except Exception as e:
            writer.write(f"Stopped: {e}")
        finally:
            with lock:
                state["busy"] = False
                state["rev"] += 1
    with lock:
        if state["busy"]:
            return False
        state["busy"] = True
        state["log"] = []
    threading.Thread(target=runner, daemon=True).start()
    return True


def install_queue():
    game = game_path()
    with lock:
        items = [i for i in state["queue"] if not i["error"]]
    for item in items:
        sm2ddr.warnings_seen.clear()
        args = types.SimpleNamespace(
            songs=item["folder"], target="a3", pack="custom_songs",
            template_song=config["template"], mcode=None,
            series=item["series"], bemaniflag=item["flag"] or None,
            tps=sm2ddr.DEFAULT_TPS, no_audio=False)
        if sm2ddr.process(args, game) == 0:
            with lock:
                state["queue"] = [i for i in state["queue"]
                                  if i["id"] != item["id"]]
    print("Finished. Start the game to play your songs.")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, data, kind="application/json", status=200):
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def local(self):
        return self.headers.get("Host") in (f"127.0.0.1:{PORT}",
                                            f"localhost:{PORT}")

    def do_GET(self):
        if not self.local():
            return self.reply({"error": "forbidden"}, status=403)
        if self.path == "/":
            page = PAGE.replace("__TOKEN__", TOKEN)
            return self.reply(page.encode(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            with lock:
                info = {"queue": list(state["queue"]), "busy": state["busy"],
                        "log": list(state["log"]), "rev": state["rev"]}
            return self.reply({**checks(), **info})
        if self.path == "/api/library":
            return self.reply(library())
        self.reply({"error": "not found"}, status=404)

    def do_POST(self):
        if not self.local() or self.headers.get("X-Token") != TOKEN:
            return self.reply({"error": "forbidden"}, status=403)
        size = int(self.headers.get("Content-Length", 0))
        if size > MAX_UPLOAD:
            return self.reply({"error": "That file is too large."}, status=413)
        try:
            if self.path == "/api/upload":
                return self.reply(self.upload(size))
            body = json.loads(self.rfile.read(size) or b"{}")
            return self.reply(self.action(self.path, body))
        except (sm2ddr.ConvertError, ValueError, OSError,
                zipfile.BadZipFile) as e:
            return self.reply({"error": str(e)}, status=400)

    def upload(self, size):
        folder = Path(tempfile.mkdtemp(dir=STAGE))
        archive = folder / "upload.zip"
        with archive.open("wb") as out:
            left = size
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                out.write(chunk)
                left -= len(chunk)
        unpacked = folder / "song"
        unpacked.mkdir()
        extract_zip(archive, unpacked)
        archive.unlink()
        added = add_to_queue(unpacked)
        if not added:
            raise sm2ddr.ConvertError("No .sm or .ssc song found in that zip.")
        return {"added": added}

    def action(self, path, body):
        if path == "/api/game":
            config["game"] = body.get("path", "").strip().strip('"')
            save_config()
            state["rev"] += 1
            return {"ok": True}
        if path == "/api/ffmpeg":
            text = body.get("path", "")
            if not use_ffmpeg(text):
                raise sm2ddr.ConvertError(
                    "No ffmpeg.exe there. Pick ffmpeg.exe or the folder it's in.")
            config["ffmpeg"] = text.strip().strip('"')
            save_config()
            state["rev"] += 1
            return {"ok": True}
        if path == "/api/folder":
            folder = Path(body.get("path", "").strip().strip('"'))
            if not folder.is_dir():
                raise sm2ddr.ConvertError("That folder doesn't exist.")
            if not add_to_queue(folder):
                raise sm2ddr.ConvertError("No .sm or .ssc songs found there.")
            return {"ok": True}
        if path == "/api/dequeue":
            with lock:
                state["queue"] = [i for i in state["queue"]
                                  if i["id"] != body.get("id")]
            return {"ok": True}
        if path == "/api/place":
            with lock:
                for item in state["queue"]:
                    if item["id"] == body.get("id"):
                        item["series"] = int(body["series"])
                        item["flag"] = int(body["flag"])
            return {"ok": True}
        game = game_path()
        if path == "/api/install":
            return {"started": run_job(install_queue)}
        if path == "/api/place-all":
            with lock:
                for item in state["queue"]:
                    item["series"] = int(body["series"])
                    item["flag"] = int(body["flag"])
            return {"ok": True}
        if path == "/api/category":
            name = " ".join(body.get("name", "").split())[:28]
            flag = int(body.get("flag", 0))
            if not name:
                raise sm2ddr.ConvertError("Type a name for the folder.")
            if flag not in sm2ddr.FOLDER_TEXTURES:
                raise sm2ddr.ConvertError("Pick a folder to rename.")

            def work():
                count = sm2ddr.set_folder_label(game, flag, name)
                config["folderNames"][str(flag)] = name
                save_config()
                print(f"Renamed the folder to {name} ({count} label files).")
            return {"started": run_job(work)}
        if path == "/api/a3":
            enable = bool(body.get("enable"))

            def work():
                state = sm2ddr.set_a3_patch(game, enable)
                print("DDR A3 folder is " + ("on." if state == "patched"
                                              else "off."))
            return {"started": run_job(work)}
        if path == "/api/apply":
            hide, show = body.get("hide", []), body.get("show", [])

            def work():
                sm2ddr.set_hidden(game, hide, show)
                print(f"Done: {len(hide)} taken out, {len(show)} put back.")
            return {"started": run_job(work)}
        if path == "/api/delete":
            base = body.get("base", "")

            def work():
                sm2ddr.delete_song(game, base)
                print(f"Permanently deleted {body.get('title', base)}.")
            return {"started": run_job(work)}
        if path == "/api/world":
            world = body.get("path", "").strip().strip('"')
            replace = bool(body.get("replace"))
            config["world"] = world
            save_config()

            def work():
                sm2ddr.import_world(game, world, replace)
            return {"started": run_job(work)}
        if path == "/api/repair":
            def work():
                entries = sm2ddr.tracked_songs(game)
                sm2ddr.rebuild_game_lists(game, entries)
                print(f"Song list restored with {len(entries)} custom songs.")
            return {"started": run_job(work)}
        raise sm2ddr.ConvertError("Unknown action.")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DDR Song Manager</title>
<style>
:root{--bg:#10131a;--panel:#191e29;--line:#2c3443;--ink:#eef1f6;--dim:#9aa5b8;
--accent:#ff4fa3;--accent2:#35d0ff;--ok:#54dd9b;--bad:#ff7b72;--warn:#ffc65c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 "Segoe UI",system-ui,sans-serif}
main{max-width:860px;margin:auto;padding:28px 18px 60px}
h1{font-size:27px;margin:0;letter-spacing:-.3px}
h1 span{color:var(--accent)}
.sub{color:var(--dim);margin:2px 0 22px}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:18px;margin-bottom:16px}
h2{font-size:16px;margin:0 0 12px}
.row{display:flex;gap:8px}
input{flex:1;min-width:0;background:#0d1016;color:var(--ink);padding:10px 12px;
border:1px solid var(--line);border-radius:8px;font:inherit}
input:focus,button:focus-visible{outline:2px solid var(--accent2);outline-offset:1px}
button{background:#2a3242;color:var(--ink);border:0;border-radius:8px;
padding:10px 14px;font:inherit;font-weight:600;cursor:pointer}
button:hover{background:#364155}
button:disabled{opacity:.45;cursor:not-allowed}
.primary{background:var(--accent);color:#22040f;width:100%;padding:13px;
font-size:16px;margin-top:12px}
.primary:hover{background:#ff71b6}
.link{background:none;color:var(--dim);padding:6px 8px;font-weight:500}
.link:hover{background:none;color:var(--bad)}
#drop{display:block;border:2px dashed var(--line);border-radius:10px;padding:26px;
text-align:center;color:var(--dim);cursor:pointer;margin-bottom:10px}
#drop.over{border-color:var(--accent2);color:var(--ink)}
#drop b{color:var(--ink)}
.song{display:flex;gap:12px;align-items:center;padding:11px 0;
border-top:1px solid var(--line)}
.song:first-child{border-top:0}
.song>div{flex:1;min-width:0}
.title{font-weight:600;overflow-wrap:anywhere}
.meta{color:var(--dim);font-size:13.5px}
.note{color:var(--warn);font-size:13.5px}
.err{color:var(--bad);font-size:13.5px}
.status{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:10px;font-size:13.5px}
.good{color:var(--ok)}.miss{color:var(--bad)}
.empty{color:var(--dim)}
pre{background:#0d1016;border-radius:8px;padding:12px;margin:0;max-height:240px;
overflow:auto;white-space:pre-wrap;font:13px/1.5 Consolas,monospace}
.help{color:var(--dim);font-size:13.5px;margin:10px 0 0}
#msg{color:var(--bad);font-size:14px;margin-top:8px;min-height:1px}
select{background:#0d1016;color:var(--ink);padding:10px;border:1px solid var(--line);
border-radius:8px;font:inherit;max-width:45%}
.filters{margin-bottom:8px}
.tag{font-size:12px;border-radius:5px;padding:1px 6px;margin-left:6px;font-weight:600;
background:#123b4a;color:var(--accent2)}
.tag.off{background:#47262a;color:var(--bad)}
.gone .title{color:var(--dim);text-decoration:line-through}
.fixed{color:var(--dim);font-size:13px;white-space:nowrap}
#more{width:100%;margin-top:8px}
.song input[type=checkbox]{flex:none;width:19px;height:19px;accent-color:var(--accent)}
.place{display:flex;flex-wrap:wrap;gap:10px;margin-top:8px}
.pick{display:flex;flex-direction:column;gap:3px;color:var(--dim);font-size:13px;
flex:1 1 200px;min-width:0}
.pick select{max-width:100%;width:100%}
#catSlot{max-width:40%}
#allBar{align-items:flex-end;padding-bottom:12px;margin-bottom:6px;
border-bottom:1px solid var(--line)}
#applyBar{position:sticky;bottom:10px}
</style></head><body><main>
<h1>DDR <span>Song Manager</span></h1>
<p class="sub">Add, organize and remove songs in your omnimix or DDR WORLD.</p>

<section><h2>1. Game folder</h2>
<div class="row"><input id="game" aria-label="Game folder"
placeholder="C:\...\MDX-001-2022020200\contents">
<button id="saveGame">Save</button></div>
<div class="status" id="status"></div>
<div class="row" style="margin-top:12px"><input id="ffmpeg" aria-label="ffmpeg location"
placeholder="ffmpeg.exe location, such as C:\ffmpeg\bin\ffmpeg.exe">
<button id="saveFfmpeg">Use this ffmpeg</button></div>
<p class="help" id="ffmpegNow"></p></section>

<section><h2>2. Add songs</h2>
<label id="drop"><input id="file" type="file" accept=".zip" multiple hidden>
<b>Drop song zips here</b> or click to choose</label>
<div class="row"><input id="folder" aria-label="Songs folder"
placeholder="Or paste a folder of songs">
<button id="addFolder">Add folder</button></div>
<div id="msg" role="alert"></div></section>

<section><h2>3. Choose where songs go</h2>
<div id="allBar" class="place" hidden></div>
<div id="queue"></div>
<button class="primary" id="install">Install songs</button>
<p class="help">Close the game before installing.</p></section>

<section id="catBox"><h2>Categories</h2>
<div class="row"><select id="catSlot" aria-label="Folder to rename"></select>
<input id="catName" aria-label="New folder name" placeholder="New name">
<button id="addCat">Rename</button></div>
<div class="song"><div><div class="title">DDR WORLD and A3 folders</div>
<div class="meta" id="a3state"></div></div><button id="a3btn"></button></div>
<div id="cats"></div>
<p class="help">Renames a CS VERSION folder in the game, such as a PLACEHOLDER
folder. Then pick it as a song's CS VERSION folder. The original labels are kept as
.bak files. Close the game first.</p>
<div class="song"><div><div class="title">Import from DDR WORLD</div>
<div class="meta">Copies the official A3 and WORLD songs from a DDR WORLD install
into the A3 and WORLD folders.</div>
<div class="row" style="margin-top:8px"><input id="world" aria-label="DDR WORLD folder"
placeholder="C:\...\MDX-2025061002"><button id="worldBtn">Import</button></div>
<label class="meta"><input type="checkbox" id="worldReplace"
style="width:auto;flex:none;vertical-align:middle"> Replace songs I converted from
StepMania with the official versions</label></div></div>
</section>

<section id="logBox" hidden><h2 id="logTitle">Progress</h2><pre id="log"></pre></section>

<section><h2>All songs <span id="count" class="meta"></span></h2>
<div class="row filters"><input id="search" aria-label="Search songs"
placeholder="Search title or artist">
<select id="cat" aria-label="Category"></select></div>
<div id="songs"></div>
<button id="more" hidden>Show more</button>
<div id="applyBar" hidden><button class="primary" id="apply"></button></div>
<p class="help">Uncheck a song to take it out of the game, and check it again to
put it back. Its data stays until you click Delete data, which can't be undone.
Songs used in courses are locked. Close the game before making changes.</p>
<p class="help">Custom songs gone after running the Install Database batch file?
<button class="link" id="repair">Restore song list</button></p></section>
</main>
<script>
const T='__TOKEN__',$=i=>document.getElementById(i);
let lastQueue='',S={},L={songs:[],versions:[],folders:[]},shown=150,rev=-1,pending={},
 listed='';
async function post(u,b,raw){
 const r=await fetch(u,{method:'POST',headers:{'X-Token':T},
  body:raw?b:JSON.stringify(b||{})});
 const j=await r.json();
 if(!r.ok)throw Error(j.error||'Something went wrong.');
 return j}
function say(t){$('msg').textContent=t||''}
async function act(f){say('');try{await f();await refresh()}catch(e){say(e.message)}}
function el(tag,cls,text){const e=document.createElement(tag);
 if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function row(s,btn,extra){const r=el('div','song'),d=el('div');
 d.append(el('div','title',s.title));
 d.append(el('div','meta',extra));
 r.append(d);if(btn)r.append(btn);return r}
function pick(text,items,value){
 const label=el('label','pick',text),select=el('select');
 for(const i of items)select.add(new Option(i.name,String(i.value)));
 select.value=String(value);label.append(select);return{label,select}}
function render(){
 if(document.activeElement!==$('game'))$('game').value=S.game||'';
 const st=$('status');st.replaceChildren();
 const c=(ok,y,n)=>st.append(el('span',ok?'good':'miss',(ok?'✓ ':'✗ ')+(ok?y:n)));
 if(S.game)c(S.gameOk,'Game found',S.problem||'Game not found');
 if(S.gameOk)c(S.templateOk,'Template song '+S.template,
  'Template song '+S.template+' is not in this game');
 c(S.ffmpeg,'ffmpeg','ffmpeg missing: put ffmpeg.exe next to this app');
 c(S.numpy,'numpy','numpy missing: run pip install numpy');
 const q=$('queue'),qkey=JSON.stringify([S.queue,L.versions,L.folders]);
 const editing=q.contains(document.activeElement);
 if(qkey!==lastQueue&&!editing){lastQueue=qkey;q.replaceChildren();
 if(!S.queue.length)q.append(el('p','empty','No songs added yet.'));
 for(const s of S.queue){
  const b=el('button','link','Remove');
  b.onclick=()=>act(()=>post('/api/dequeue',{id:s.id}));
  const r=row(s,b,[s.artist,s.bpm&&s.bpm+' BPM',s.charts.join(', ')]
   .filter(Boolean).join(' · '));
  const d=r.firstChild;
  if(!s.error){
   const p=el('div','place'),v=pick('Version folder',L.versions,s.series),
    f=pick('CS VERSION folder',[{value:0,name:'None'},...L.folders],s.flag);
   const save=()=>act(()=>post('/api/place',{id:s.id,series:v.select.value,
    flag:f.select.value}));
   v.select.onchange=f.select.onchange=save;if(S.isWorld)f.label.style.display='none';
   p.append(v.label,f.label);d.append(p)}
  if(s.error)d.append(el('div','err',s.error));
  else{const n=[];if(s.mines)n.push('mines are removed');
   if(s.rolls)n.push('rolls become freezes');
   if(n.length)d.append(el('div','note',n.join(', ')))}
  q.append(r)}
 }
 const a3=$('a3state'),ab=$('a3btn');
 a3.textContent={patched:'On. Pick "WORLD" or "A3" as a song\'s version folder.',
  original:'Off. Adds DDR WORLD and DDR A3 folders to the top of AC VERSION by patching gamemdx.dll.',
  outdated:'An older version of the patch is on. Click Update to add WORLD above A3.',
  unknown:'Not available for this gamemdx.dll.',missing:'Set the game folder first.'}[S.a3];
 ab.textContent=S.a3==='patched'?'Turn off':S.a3==='outdated'?'Update':'Turn on';
 ab.disabled=S.busy||!['patched','original','outdated'].includes(S.a3);
 ab.onclick=()=>act(()=>post('/api/a3',{enable:S.a3!=='patched'}));
 $('catBox').hidden=S.isWorld;
 const ab2=$('allBar');ab2.hidden=S.queue.filter(s=>!s.error).length<2;
 if(ab2.dataset.kind!==String(S.isWorld))ab2.replaceChildren();
 if(!ab2.hidden&&!ab2.firstChild){ab2.dataset.kind=String(S.isWorld);
  const v=pick('Version folder for all songs',L.versions,S.isWorld?21:20),
   f=pick('CS VERSION folder for all songs',[{value:0,name:'None'},...L.folders],
    S.isWorld?0:4),
   b=el('button',null,'Set all');
  b.onclick=()=>act(()=>post('/api/place-all',{series:v.select.value,flag:f.select.value}));
  if(S.isWorld)f.label.style.display='none';ab2.append(v.label,f.label,b)}
 const ok=S.gameOk&&S.templateOk&&S.ffmpeg&&S.numpy&&!S.busy;
 $('install').disabled=!ok||!S.queue.some(s=>!s.error);
 $('install').textContent=S.busy?'Working…':'Install songs';
 $('repair').disabled=!S.gameOk||S.busy;
 $('ffmpegNow').textContent=S.ffmpegPath?'Using '+S.ffmpegPath:
  'Paste where ffmpeg.exe is, or the folder it\'s in, then click Use this ffmpeg.';
 $('worldBtn').disabled=S.busy||S.a3!=='patched';
 if(!$('world').value&&S.world&&document.activeElement!==$('world'))$('world').value=S.world;
 $('logBox').hidden=!S.log.length&&!S.busy;
 $('log').textContent=S.log.join('\n');$('log').scrollTop=1e6;
}
function renderSongs(){
 const q=$('search').value.toLowerCase(),c=$('cat').value;
 const list=L.songs.filter(s=>(!c||(c==='custom'?s.custom:c==='hidden'?s.hidden:
  c[0]==='f'?(s.flag&Number(c.slice(1)))>0:String(s.series)===c))&&(s.title+' '+s.artist).toLowerCase().includes(q));
 $('count').textContent='('+list.length.toLocaleString()+')';
 const g=$('songs');g.replaceChildren();
 if(!list.length)g.append(el('p','empty',L.songs.length?'No matching songs.':
  'Set your game folder to see songs.'));
 for(const s of list.slice(0,shown)){
  const want=s.base in pending?pending[s.base]:!s.hidden;
  const r=el('div','song'),box=el('input');
  box.type='checkbox';box.checked=want;box.title='In the game';
  box.setAttribute('aria-label','In the game: '+s.title);
  box.disabled=S.busy||(s.course&&!s.hidden);
  box.onchange=()=>{if(box.checked===!s.hidden)delete pending[s.base];
   else pending[s.base]=box.checked;renderSongs()};
  const d=el('div');const t=el('div','title',s.title);
  if(s.custom)t.append(el('span','tag','Custom'));
  if(!want)t.append(el('span','tag off','Out of game'));
  d.append(t,el('div','meta',[s.artist,s.category,s.folder,s.top&&'Level '+s.top]
   .filter(Boolean).join(' · ')));
  r.append(box,d);if(!want)r.classList.add('gone');
  if(s.course)r.append(el('span','fixed','In a course'));
  else{const b=el('button','link','Delete data');b.disabled=S.busy;
   b.onclick=()=>{if(confirm('Permanently delete '+s.title+
    ' and all its files? This can\'t be undone.'))
    act(()=>post('/api/delete',{base:s.base,title:s.title}))};r.append(b)}
  g.append(r)}
 const n=Object.keys(pending).length;
 $('applyBar').hidden=!n;
 $('apply').disabled=S.busy;
 $('apply').textContent='Apply '+n+(n===1?' change':' changes');
 $('more').hidden=list.length<=shown;
}
async function loadLibrary(){
 L=await (await fetch('/api/library')).json();
 const c=$('cat'),v=c.value,seen=new Map();
 L.songs.forEach(s=>seen.set(s.series,s.category));
 c.replaceChildren(new Option('All categories',''),new Option('Custom songs','custom'),
  new Option('Out of game','hidden'));
 [...seen].sort((a,b)=>a[0]-b[0]).forEach(([n,t])=>c.add(new Option(t,String(n))));
 L.folders.filter(f=>f.count).forEach(f=>c.add(new Option(f.name,'f'+f.value)));
 c.value=v;
 const k=$('cats'),cs=$('catSlot'),cv=cs.value;k.replaceChildren();cs.replaceChildren();
 for(const f of L.folders){cs.add(new Option(f.name,String(f.value)));
  const r=el('div','song'),d=el('div');
  d.append(el('div','title',f.name),el('div','meta',f.count+(f.count===1?' song':' songs')));
  r.append(d);k.append(r)}
 if(cv)cs.value=cv;
 renderSongs()}
async function refresh(){S=await (await fetch('/api/state')).json();render();
 if(S.rev!==rev){rev=S.rev;await loadLibrary();render()}}
async function upload(files){for(const f of files)
 await act(()=>post('/api/upload',f,true))}
$('search').oninput=()=>{shown=150;renderSongs()};
$('cat').onchange=()=>{shown=150;renderSongs()};
$('more').onclick=()=>{shown+=150;renderSongs()};
$('apply').onclick=()=>act(async()=>{const hide=[],show=[];
 for(const[b,on]of Object.entries(pending))(on?show:hide).push(b);
 await post('/api/apply',{hide,show});pending={}});
$('addCat').onclick=()=>act(async()=>{await post('/api/category',
 {name:$('catName').value,flag:$('catSlot').value});$('catName').value=''});
$('saveFfmpeg').onclick=()=>act(()=>post('/api/ffmpeg',{path:$('ffmpeg').value}));
$('saveGame').onclick=()=>act(()=>post('/api/game',{path:$('game').value}));
$('addFolder').onclick=()=>act(async()=>{await post('/api/folder',
 {path:$('folder').value});$('folder').value=''});
$('install').onclick=()=>act(()=>post('/api/install'));
$('worldBtn').onclick=()=>act(()=>post('/api/world',{path:$('world').value,
 replace:$('worldReplace').checked}));
$('repair').onclick=()=>act(()=>post('/api/repair'));
$('file').onchange=e=>{upload(e.target.files);e.target.value=''};
const D=$('drop');
['dragenter','dragover'].forEach(n=>D.addEventListener(n,e=>{e.preventDefault();
 D.classList.add('over')}));
['dragleave','drop'].forEach(n=>D.addEventListener(n,e=>{e.preventDefault();
 D.classList.remove('over')}));
D.addEventListener('drop',e=>upload(e.dataTransfer.files));
refresh();setInterval(()=>{refresh().catch(()=>{})},1500);
</script></body></html>
"""


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"DDR Song Manager is running at {url}")
    print("Keep this window open. Close it when you're done.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(STAGE, ignore_errors=True)


if __name__ == "__main__":
    main()

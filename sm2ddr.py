#!/usr/bin/env python3
"""sm2ddr - convert StepMania songs (.sm/.ssc) into DDR arcade game data.

Commands:
  inspect GAME_DIR              report the layout of a DDR install
  dump-ssq FILE.ssq             print the chunks of an SSQ file
  build SONGS --out DIR         convert songs into a standalone folder
  install SONGS --game GAME_DIR convert songs into GAME_DIR/data_mods/<pack>
  install SONGS --game GAME_DIR --target a3 --template-song NAME
                                write into data/ and update startup.arc

World installs never modify stock files. Direct installs rebuild startup.arc,
leaving startup_eng (or a .bak copy) untouched. Needs Python 3.9+ and, for audio and
jackets, ffmpeg on PATH and numpy. See README.md for what is and isn't verified.
"""
import argparse
import collections
import hashlib
import json
import math
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

MEASURE = 4096                  # SSQ offset units per 4/4 measure
UNITS_PER_BEAT = MEASURE // 4
DEFAULT_TPS = 1000              # SSQ tempo ticks per second
ARC_MAGIC = bytes.fromhex("20117519")
MUSICDB_ARC_PATH = "data/gamedata/musicdb.xml"  # used if not found by name
LAYOUT_SSQ = "mdb_apx/ssq"      # relative to data/ (confirmed for World)
LAYOUT_AUDIO = "sound/win/dance"  # relative to data/ (reported for DDR A)

# SM difficulty name -> (SSQ difficulty id, index within a 5-slot diffLv row)
DIFFICULTIES = {
    "beginner": (4, 0),
    "easy": (1, 1),
    "medium": (2, 2),
    "hard": (3, 3),
    "challenge": (6, 4),
}
STYLES = {"dance-single": (0x14, 4, 0), "dance-double": (0x18, 8, 5)}

warnings_seen = []


def warn(msg):
    warnings_seen.append(msg)
    print(f"  warning: {msg}", file=sys.stderr)


class ConvertError(Exception):
    pass


# --------------------------------------------------------------- simfiles


@dataclass
class Chart:
    style: str
    difficulty: str
    meter: int
    notes: str
    has_own_timing: bool = False


@dataclass
class Song:
    path: Path
    title: str = ""
    subtitle: str = ""
    artist: str = ""
    music: str = ""
    offset: float = 0.0
    sample_start: float = -1.0
    sample_length: float = 15.0
    bpms: list = field(default_factory=list)   # (beat, bpm)
    stops: list = field(default_factory=list)  # (beat, seconds)
    images: list = field(default_factory=list)
    charts: list = field(default_factory=list)


def read_text(path):
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp932", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ConvertError(f"cannot decode {path}")


def parse_msd(text):
    """Yield (TAG, value) pairs from MSD text, tolerating a missing ';'."""
    text = re.sub(r"//[^\n]*", "", text)
    i, n = 0, len(text)
    while True:
        i = text.find("#", i)
        if i < 0:
            return
        colon = text.find(":", i)
        if colon < 0:
            return
        end = colon + 1
        while end < n and text[end] != ";":
            if text[end] == "#" and text[end - 1] == "\n":
                break
            end += 1
        yield text[i + 1:colon].strip().upper(), text[colon + 1:end].strip()
        i = end


def parse_pairs(value):
    out = []
    for item in re.split(r"[,\n]", value):
        item = item.strip()
        if "=" not in item:
            continue
        a, b = item.split("=")[:2]
        try:
            out.append((float(a), float(b)))
        except ValueError:
            warn(f"ignoring malformed timing entry {item!r}")
    return out


def num(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_simfile(path):
    song = Song(path=path)
    chart = None
    delays = []
    for tag, value in parse_msd(read_text(path)):
        if tag == "TITLE":
            song.title = value
        elif tag == "SUBTITLE":
            song.subtitle = value
        elif tag == "ARTIST":
            song.artist = value
        elif tag == "MUSIC" and chart is None:
            song.music = value
        elif tag == "OFFSET" and chart is None:
            song.offset = num(value, 0.0)
        elif tag == "SAMPLESTART":
            song.sample_start = num(value, -1.0)
        elif tag == "SAMPLELENGTH":
            song.sample_length = num(value, 15.0) or 15.0
        elif tag in ("JACKET", "BANNER", "BACKGROUND") and value:
            song.images.append((tag, value))
        elif tag == "NOTEDATA":
            chart = Chart("", "", 1, "")
            song.charts.append(chart)
        elif chart is not None and tag == "STEPSTYPE":
            chart.style = value.lower()
        elif chart is not None and tag == "DIFFICULTY":
            chart.difficulty = value.lower()
        elif chart is not None and tag == "METER":
            chart.meter = int(num(value, 1))
        elif chart is not None and tag == "NOTES":
            chart.notes = value
        elif chart is not None and tag in ("BPMS", "STOPS", "OFFSET"):
            chart.has_own_timing = True
        elif tag == "BPMS":
            song.bpms = parse_pairs(value)
        elif tag == "STOPS" or tag == "FREEZES":
            song.stops += parse_pairs(value)
        elif tag == "DELAYS":
            delays = parse_pairs(value)
        elif tag == "WARPS" and parse_pairs(value):
            raise ConvertError("uses warps, which SSQ timing cannot represent")
        elif tag == "NOTES":
            parts = value.split(":")
            if len(parts) >= 6:
                song.charts.append(Chart(
                    parts[0].strip().lower(), parts[2].strip().lower(),
                    int(num(parts[3].strip(), 1)), ":".join(parts[5:])))
    if delays:
        warn("delays are treated as stops; notes on those beats land early")
        song.stops += delays
    if not song.bpms:
        raise ConvertError("has no BPMS")
    if any(b <= 0 for _, b in song.bpms) or any(s < 0 for _, s in song.stops):
        raise ConvertError("uses negative BPMs or stops (a warp gimmick)")
    song.bpms.sort()
    song.stops = sorted(s for s in song.stops if s[1] > 0)
    if song.bpms[0][0] > 0:
        song.bpms[0] = (0.0, song.bpms[0][1])
    return song


# ---------------------------------------------------------------- charts


def parse_notes(text, panels):
    """Return (steps {offset: mask}, freeze tails {offset: mask}, mines)."""
    steps, tails = {}, {}
    open_holds = {}
    mines = rolls = 0
    measures = re.sub(r"\s+", "", text).split(",")
    for m, measure in enumerate(measures):
        if not measure:
            continue
        if len(measure) % panels:
            raise ConvertError(f"measure {m} is not a multiple of {panels}")
        rows = len(measure) // panels
        for r in range(rows):
            offset = m * MEASURE + round(r * MEASURE / rows)
            for p in range(panels):
                c = measure[r * panels + p].upper()
                if c in "1L":
                    steps[offset] = steps.get(offset, 0) | 1 << p
                elif c in "24":
                    steps[offset] = steps.get(offset, 0) | 1 << p
                    open_holds[p] = offset
                    rolls += c == "4"
                elif c == "3":
                    if p in open_holds:
                        open_holds.pop(p)
                        tails[offset] = tails.get(offset, 0) | 1 << p
                elif c == "M":
                    mines += 1
    for p, head in open_holds.items():
        warn(f"unclosed hold at offset {head}; keeping it as a tap")
    if rolls:
        warn(f"{rolls} rolls became freeze arrows")
    return steps, tails, mines


def seconds_at(song, beat):
    """Song time in seconds of a beat, with OFFSET applied."""
    t = -song.offset
    prev, bpm = 0.0, song.bpms[0][1]
    for b, new in song.bpms[1:]:
        if b >= beat:
            break
        t += (b - prev) * 60 / bpm
        prev, bpm = b, new
    t += (beat - prev) * 60 / bpm
    return t + sum(s for b, s in song.stops if b < beat)


def build_tempo(song, end_offset, tps, shift, lead_units):
    """Return parallel lists (offsets, ticks) for the tempo chunk."""
    end_beat = (end_offset - lead_units) / UNITS_PER_BEAT
    events = {b for b, _ in song.bpms} | {b for b, _ in song.stops} | {0.0}
    entries = [(0, 0)] if lead_units else []
    for beat in sorted(b for b in events if b < end_beat) + [end_beat]:
        off = round(beat * UNITS_PER_BEAT) + lead_units
        t = seconds_at(song, beat) + shift
        entries.append((off, round(t * tps)))
        stop = sum(s for b, s in song.stops if b == beat)
        if stop:
            entries.append((off, round((t + stop) * tps)))
    return [e[0] for e in entries], [e[1] for e in entries]


def chunk(kind, p2, count, body):
    body += b"\0" * (-len(body) % 4)
    return struct.pack("<IHHHH", 12 + len(body), kind, p2, count, 0) + body


def tempo_chunk(offsets, ticks, tps):
    n = len(offsets)
    return chunk(1, tps, n, struct.pack(f"<{n}i{n}i", *offsets, *ticks))


def step_chunk(style_id, diff_id, steps, tails):
    entries = sorted(
        [(o, 0, m) for o, m in tails.items()] +
        [(o, 1, m) for o, m in steps.items()])
    n = len(entries)
    body = struct.pack(f"<{n}i", *[e[0] for e in entries])
    body += bytes(0 if e[1] == 0 else e[2] for e in entries)
    body += b"\0" * (len(body) & 1)  # freeze data is 2-byte aligned
    for _, kind, mask in entries:
        if kind == 0:
            body += bytes((mask, 1))
    return chunk(3, diff_id << 8 | style_id, n, body)


def trigger_chunk(first, last):
    """Six song events, positioned as in real DDR charts."""
    start = max(0, first // MEASURE - 1) * MEASURE
    end = (math.ceil(last / MEASURE) + 1) * MEASURE
    events = [(0, 0x0401), (0, 0x0102), (max(0, start - MEASURE), 0x0202),
              (start, 0x0502), (end, 0x0302), (end + MEASURE, 0x0402)]
    n = len(events)
    return chunk(2, 1, n, struct.pack(f"<{n}i{n}H", *[e[0] for e in events],
                                      *[e[1] for e in events])), end + MEASURE


def read_ssq(data):
    """Parse an SSQ into a list of chunk dicts."""
    pos, chunks = 0, []
    while pos + 4 <= len(data):
        size = struct.unpack_from("<I", data, pos)[0]
        if size == 0:
            break
        kind, p2, count, p4 = struct.unpack_from("<4H", data, pos + 4)
        body = data[pos + 12:pos + size]
        c = dict(kind=kind, p2=p2, count=count, p4=p4, size=size)
        if kind in (1, 2, 3):
            c["offsets"] = list(struct.unpack_from(f"<{count}i", body))
        if kind == 1:
            c["ticks"] = list(struct.unpack_from(f"<{count}i", body, count * 4))
        elif kind == 2:
            c["codes"] = list(struct.unpack_from(f"<{count}H", body, count * 4))
        elif kind == 3:
            c["steps"] = list(body[count * 4:count * 5])
            extra = count * 5 + (count & 1)
            c["extra"] = list(body[extra:extra + 2 * c["steps"].count(0)])
        chunks.append(c)
        pos += size
    return chunks


def convert_charts(song, args):
    """Return (ssq bytes, diffLv list, shift seconds)."""
    seen, built, levels = set(), [], [0] * 10
    for chart in song.charts:
        if chart.style not in STYLES:
            continue
        if chart.difficulty not in DIFFICULTIES:
            warn(f"skipping {chart.difficulty or 'unnamed'} chart")
            continue
        key = (chart.style, chart.difficulty)
        if key in seen:
            warn(f"duplicate {key[0]} {key[1]} chart skipped")
            continue
        seen.add(key)
        if chart.has_own_timing:
            warn(f"{key[1]} has its own timing; using the song timing")
        style_id, panels, base = STYLES[chart.style]
        diff_id, slot = DIFFICULTIES[chart.difficulty]
        if key == ("dance-double", "beginner"):
            warn("doubles Beginner charts do not exist on DDR; skipped")
            continue
        steps, tails, mines = parse_notes(chart.notes, panels)
        if not steps:
            warn(f"{key[0]} {key[1]} chart is empty, skipped")
            continue
        if mines:
            warn(f"{mines} mines removed from {key[1]}")
        meter = max(1, min(19, chart.meter))
        if meter != chart.meter:
            warn(f"meter {chart.meter} clamped to {meter}")
        levels[base + slot] = meter
        built.append((style_id, diff_id, steps, tails))
    if not built:
        raise ConvertError("has no usable dance-single or dance-double charts")
    first = min(min(b[2]) for b in built)
    last = max(max(list(b[2]) + list(b[3])) for b in built)
    # Real charts start well after offset 0 and their tempo table starts at
    # (0, 0), so add silent measures: keep at least two before the first note.
    bpm = song.bpms[0][1]
    lead = max(0, math.ceil((2 * MEASURE - first) / MEASURE))
    while lead * 240 / bpm + song.offset < 0:
        lead += 1
    lead_units = lead * MEASURE
    shift = lead * 240 / bpm + song.offset
    triggers, end = trigger_chunk(first + lead_units, last + lead_units)
    offsets, ticks = build_tempo(song, end, args.tps, shift, lead_units)
    data = tempo_chunk(offsets, ticks, args.tps) + triggers
    for style_id, diff_id, steps, tails in sorted(built, key=lambda b: b[:2]):
        data += step_chunk(style_id, diff_id,
                           {o + lead_units: m for o, m in steps.items()},
                           {o + lead_units: m for o, m in tails.items()})
    return data + b"\0\0\0\0", levels, shift


# ------------------------------------------------------------- ARC + LZ77


def lz77_decompress(data):
    out = bytearray()
    pos = 0
    while pos < len(data):
        flags = data[pos]
        pos += 1
        for bit in range(8):
            if flags >> bit & 1:
                out.append(data[pos])
                pos += 1
                continue
            word = data[pos] << 8 | data[pos + 1]
            pos += 2
            distance = word >> 4
            if distance == 0:
                return bytes(out)
            for _ in range((word & 15) + 3):
                out.append(out[-distance] if distance <= len(out) else 0)
    return bytes(out)


def lz77_compress(data):
    """Compress in the ARC LZ77 format, using a hash chain for matches."""
    out = bytearray()
    group = bytearray()
    flags = bits = 0

    def token(literal, payload):
        nonlocal flags, bits
        flags |= literal << bits
        group.extend(payload)
        bits += 1
        if bits == 8:
            out.append(flags)
            out.extend(group)
            group.clear()
            flags = bits = 0

    chains = {}
    pos, n = 0, len(data)
    while pos < n:
        key = data[pos:pos + 3]
        best_len = best_dist = 0
        for cand in reversed(chains.get(key, [])[-32:]):
            dist = pos - cand
            if dist > 4095:
                break
            length = 0
            while length < 18 and pos + length < n and \
                    data[cand + length] == data[pos + length]:
                length += 1
            if length > best_len:
                best_len, best_dist = length, dist
        step = best_len if best_len >= 3 else 1
        if best_len >= 3:
            word = best_dist << 4 | best_len - 3
            token(0, bytes((word >> 8, word & 255)))
        else:
            token(1, data[pos:pos + 1])
        for i in range(pos, min(pos + step, n)):
            chains.setdefault(data[i:i + 3], []).append(i)
        pos += step
    token(0, b"\0\0")
    if bits:
        out.append(flags)
        out.extend(group)
    return bytes(out)


def read_arc(path):
    """Return {name: (offset, stored size, full size)} and the file bytes."""
    data = Path(path).read_bytes()
    if data[:4] != ARC_MAGIC:
        raise ConvertError(f"{path} is not an ARC archive")
    _, count, _ = struct.unpack_from("<III", data, 4)
    files = {}
    for i in range(count):
        name_off, off, full, stored = struct.unpack_from(
            "<IIII", data, 0x10 + i * 0x10)
        end = data.index(b"\0", name_off)
        files[data[name_off:end].decode("utf-8")] = (off, stored, full)
    return files, data


def arc_get(files, data, name):
    off, stored, full = files[name]
    raw = data[off:off + stored]
    return raw if stored == full else lz77_decompress(raw)


def write_arc(files, stored=None):
    """Build an ARC, uncompressed unless a file is given in `stored`.

    `stored` maps names to (compressed bytes, full size). Compressed jacket
    ARCs reportedly crash, so only startup.arc uses it.
    """
    stored = stored or {}
    names = b""
    name_base = 0x10 + len(files) * 0x10
    name_offsets = []
    for name in files:
        name_offsets.append(name_base + len(names))
        names += name.encode("utf-8") + b"\0"
    pos = name_base + len(names)
    pos += -pos % 64
    table, blob = b"", b""
    items = list(files.items())
    for index, (name_off, (name, content)) in enumerate(
            zip(name_offsets, items)):
        full = stored[name][1] if name in stored else len(content)
        table += struct.pack("<IIII", name_off, pos + len(blob), full,
                             len(content))
        blob += content
        if index < len(items) - 1:  # stock archives leave the last file bare
            blob += b"\0" * (-len(content) % 64)
    head = ARC_MAGIC + struct.pack("<III", 1, len(files), 2) + table + names
    return head + b"\0" * (pos - len(head)) + blob


# ---------------------------------------------------------------- musicdb


def decode_xml(raw):
    head = raw[:200].decode("latin-1")
    m = re.search(r'encoding=["\']([\w-]+)', head)
    declared = m.group(1) if m else "utf-8"
    enc = "cp932" if declared.lower() in ("shift_jis", "shift-jis", "sjis") \
        else declared
    text = raw.decode(enc, "replace")
    return re.sub(r"^\s*<\?xml[^>]*\?>", "", text), enc, declared


def database_file(game):
    """Return (live file, untouched source). startup.arc comes first.

    Omnimix installs keep their database in startup_eng and copy it to
    startup.arc, so startup_eng counts as the untouched source.
    """
    arc_dir = game / "data" / "arc"
    if (arc_dir / "startup_eng").exists():
        return arc_dir / "startup.arc", arc_dir / "startup_eng"
    data = game / "data"
    hits = sorted(data.rglob("startup.arc")) or sorted(data.rglob("musicdb.xml"))
    if not hits:
        return None
    return hits[0], hits[0].with_name(hits[0].name + ".bak")


def read_database(path):
    """Return (raw xml bytes, name inside the archive or None)."""
    if path.name.endswith((".arc", ".arc.bak")) or path.name == "startup_eng":
        files, data = read_arc(path)
        name = next((n for n in files if n.endswith("musicdb.xml")), None)
        return (arc_get(files, data, name), name) if name else (None, None)
    return path.read_bytes(), None


def load_musicdb(game):
    """Return (root element, arc path, encoding name, declared) or None."""
    found = database_file(game)
    if not found:
        return None
    live, backup = found
    raw, name = read_database(backup if backup.exists() else live)
    if raw is None:
        return None
    text, enc, declared = decode_xml(raw)
    return ET.fromstring(text), name or MUSICDB_ARC_PATH, enc, declared


def set_field(entry, tag, value, kind=None):
    el = entry.find(tag)
    if el is None:
        el = ET.SubElement(entry, tag)
        if kind:
            el.set("__type", kind)
    el.text = str(value)
    return el


def sort_key(title):
    """Mimic title_yomi: lowercase, parentheses as ', other symbols dropped."""
    text = re.sub(r"[()\[\]]", "'", title.lower())
    return re.sub(r"[^\w']", "", text)


def make_entry(template, mcode, basename, song, levels, series,
               available="0", bemaniflag=None):
    """Build a clean entry in stock field order.

    Only the series comes from the template. Fields such as eventno, property
    and region are left out on purpose because they lock or restrict songs.
    """
    entry = ET.Element("music")
    title = song.title + (f" {song.subtitle}" if song.subtitle else "")
    bpms = [b for _, b in song.bpms]
    low, high = round(min(bpms)), round(max(bpms))
    if series is None:
        series = template.findtext("series") if template is not None else 21
    set_field(entry, "mcode", mcode, "u32")
    set_field(entry, "basename", basename)
    set_field(entry, "title", title)
    set_field(entry, "title_yomi", sort_key(title))
    set_field(entry, "artist", song.artist)
    if low != high:
        set_field(entry, "bpmmin", low, "u16")
    set_field(entry, "bpmmax", high, "u16")
    set_field(entry, "series", series, "u8")
    if bemaniflag is not None:
        set_field(entry, "bemaniflag", bemaniflag, "u32")
    if template is not None and template.find("bgstage") is not None:
        set_field(entry, "bgstage", template.findtext("bgstage"), "u16")
    # limited_ary: -1 means no chart. Other charts use the database's own
    # normal value. Older and omnimix databases have no such field at all.
    if available is not None:
        el = set_field(entry, "limited_ary", " ".join(
            available if lv else "-1" for lv in levels), "s32")
        el.set("__count", "10")
    el = set_field(entry, "diffLv", " ".join(map(str, levels)), "u8")
    el.set("__count", "10")
    return entry


def patch_startup(game, entries):
    """Add entries to the database, always starting from the first backup."""
    found = database_file(game)
    if not found:
        raise SystemExit("no startup.arc or musicdb.xml found")
    live, backup = found
    if not backup.exists():
        shutil.copy2(live, backup)
    raw, name = read_database(backup)
    _, enc, _ = decode_xml(raw)
    text = raw.decode(enc, "replace")
    root = ET.fromstring(decode_xml(raw)[0])
    hidden = hidden_songs(game)
    removed = 0
    if hidden:
        def drop(match):
            nonlocal removed
            name = re.search(r"<basename>(.*?)</basename>", match.group(0))
            if name and name.group(1) in hidden:
                removed += 1
                return ""
            return match.group(0)
        text = re.sub(r"[ \t]*<music>.*?</music>[ \t]*\r?\n?", drop, text,
                      flags=re.S)
    close = text.rindex(f"</{root.tag}>")
    newline = "\r\n" if "\r\n" in text else "\n"
    found_indent = re.search(r"\n([ \t]+)<music>", text)
    unit = found_indent.group(1) if found_indent else "  "
    added = ""
    entries = [e for e in entries if e.findtext("basename") not in hidden]
    for entry in entries:
        entry = ET.fromstring(ET.tostring(entry))
        ET.indent(entry, space=unit, level=1)
        entry.tail = None
        block = ET.tostring(entry, encoding="unicode")
        added += unit + block.replace("\n", newline) + newline
    new_xml = (text[:close] + added + text[close:]).encode(enc, "replace")
    if name is None:
        result = new_xml
    else:
        files, data = read_arc(backup)
        contents, stored = {}, {}
        for n, (off, size, full) in files.items():
            contents[n] = data[off:off + size]
            if size != full:
                stored[n] = (contents[n], full)
        packed = lz77_compress(new_xml)
        if len(packed) < len(new_xml):
            contents[name], stored[name] = packed, (packed, len(new_xml))
        else:
            contents[name] = new_xml
            stored.pop(name, None)
        result = write_arc(contents, stored)
    temp = live.with_name(live.name + ".new")
    temp.write_bytes(result)
    check = ET.fromstring(decode_xml(read_database_for_check(temp, name))[0])
    if len(check) != len(root) + len(entries) - removed:
        temp.unlink()
        raise SystemExit("database verification failed; game not changed")
    temp.replace(live)
    return len(check)


def read_database_for_check(path, name):
    if name is None:
        return path.read_bytes()
    files, data = read_arc(path)
    return arc_get(files, data, name)


def write_fragment(path, entries, enc, declared):
    root = ET.Element("mdb")
    root.extend(entries)
    ET.indent(root)
    body = ET.tostring(root, encoding="unicode")
    text = f'<?xml version="1.0" encoding="{declared}"?>\n{body}\n'
    raw = text.encode(enc, "replace")
    if "?" in text and raw.decode(enc).count("?") > text.count("?"):
        warn(f"some title characters do not fit {declared}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


# ------------------------------------------------------------------ audio


def ffmpeg(*args):
    exe = shutil.which("ffmpeg")
    if not exe:
        raise ConvertError("ffmpeg was not found on PATH")
    run = subprocess.run([exe, "-y", "-v", "error", *args],
                         capture_output=True, text=True)
    if run.returncode:
        raise ConvertError(f"ffmpeg failed: {run.stderr.strip()[:300]}")


def find_file(folder, name, patterns):
    if name:
        target = folder / name
        if target.is_file():
            return target
        for f in folder.iterdir():
            if f.name.lower() == name.lower():
                return f
    for pattern in patterns:
        hits = sorted(folder.glob(pattern))
        if hits:
            return hits[0]
    return None


ADPCM_ADAPT = (230, 230, 230, 230, 307, 409, 512, 614,
               768, 614, 512, 409, 307, 230, 230, 230)
ADPCM_COEF = ((256, 0), (512, -256), (0, 0), (192, 64),
              (240, 0), (460, -208), (392, -232))
SAMPLES_PER_BLOCK = 128  # XACT "128 samples": 70 bytes per channel


def decode_audio(src, shift, start=None, length=None, fade=False):
    """Return 44.1 kHz stereo 16-bit samples as a numpy array."""
    try:
        import numpy as np
    except ImportError:
        raise ConvertError("audio needs numpy (run: pip install numpy)")
    exe = shutil.which("ffmpeg")
    if not exe:
        raise ConvertError("ffmpeg was not found on PATH")
    args = [exe, "-v", "error"]
    if start is not None:
        args += ["-ss", f"{start:.3f}", "-t", f"{length:.3f}"]
    filters = ["aformat=sample_rates=44100:channel_layouts=stereo"]
    if shift > 0 and start is None:
        filters.append(f"adelay={round(shift * 1000)}:all=1")
    if fade:
        filters.append(f"afade=t=out:st={max(0, length - 1):.3f}:d=1")
    run = subprocess.run(
        args + ["-i", str(src), "-af", ",".join(filters), "-f", "s16le",
                "-acodec", "pcm_s16le", "-"], capture_output=True)
    if run.returncode:
        raise ConvertError(f"ffmpeg failed: {run.stderr[:300]!r}")
    return np.frombuffer(run.stdout, "<i2").reshape(-1, 2).astype("int64")


def adpcm_encode(pcm):
    """Encode stereo samples as MS-ADPCM, 128 samples per block.

    All blocks are encoded in parallel with each of the seven predictors, and
    the best predictor is kept for each channel of each block.
    """
    import numpy as np
    blocks = max(1, math.ceil(len(pcm) / SAMPLES_PER_BLOCK))
    data = np.zeros((blocks * SAMPLES_PER_BLOCK, 2), "int64")
    data[:len(pcm)] = pcm
    x = data.reshape(blocks, SAMPLES_PER_BLOCK, 2).transpose(0, 2, 1) \
        .reshape(blocks * 2, SAMPLES_PER_BLOCK)
    adapt = np.array(ADPCM_ADAPT, "int64")
    start_delta = np.clip(np.abs(x[:, 2] - x[:, 1]) // 2, 16, 32767)
    best_err = np.full(len(x), np.iinfo("int64").max)
    best_codes = np.zeros((len(x), SAMPLES_PER_BLOCK - 2), "int64")
    best_pred = np.zeros(len(x), "int64")
    for index, (c1, c2) in enumerate(ADPCM_COEF):
        s1, s2 = x[:, 1].copy(), x[:, 0].copy()
        delta = start_delta.copy()
        err = np.zeros(len(x), "int64")
        codes = np.zeros_like(best_codes)
        for i in range(2, SAMPLES_PER_BLOCK):
            guess = np.floor_divide(s1 * c1 + s2 * c2, 256)
            diff = x[:, i] - guess
            nib = np.clip(np.rint(diff / delta).astype("int64"), -8, 7)
            value = np.clip(guess + nib * delta, -32768, 32767)
            err += (x[:, i] - value) ** 2
            codes[:, i - 2] = nib & 15
            delta = np.maximum(16, (adapt[nib & 15] * delta) // 256)
            s2, s1 = s1, value
        better = err < best_err
        best_err[better] = err[better]
        best_codes[better] = codes[better]
        best_pred[better] = index
    left, right = np.arange(blocks) * 2, np.arange(blocks) * 2 + 1
    out = bytearray()
    nibbles = np.zeros((blocks, (SAMPLES_PER_BLOCK - 2) * 2), "int64")
    nibbles[:, 0::2] = best_codes[left]
    nibbles[:, 1::2] = best_codes[right]
    packed = (nibbles[:, 0::2] << 4 | nibbles[:, 1::2]).astype("uint8")
    for b in range(blocks):
        out += struct.pack(
            "<BBhhhhhh", best_pred[left[b]], best_pred[right[b]],
            start_delta[left[b]], start_delta[right[b]],
            x[left[b], 1], x[right[b], 1], x[left[b], 0], x[right[b], 0])
        out += packed[b].tobytes()
    return bytes(out), blocks * SAMPLES_PER_BLOCK


def build_xwb(name, sounds, versions=(43, 42)):
    """Write a stock-layout wave bank from (adpcm bytes, samples) pairs."""
    align = 2048
    channels, rate, block = 2, 44100, 70
    fmt_bits = 2 | channels << 2 | rate << 5 | (block - 22) << 23
    entries, blob = [], b""
    for adpcm, samples in sounds:
        blob += b"\0" * (-len(blob) % align)
        entries.append(struct.pack("<IIIIII", (samples + 1) << 4, fmt_bits,
                                   len(blob), len(adpcm), 0, samples))
        blob += adpcm
    meta = b"".join(entries)
    bank = struct.pack("<II64sIII", 0x80001, len(sounds), name.encode()[:63],
                       24, 64, align) + b"\0" * 12
    bank_off = 12 + 5 * 8  # magic, two versions, five regions
    meta_off = bank_off + len(bank)
    seek_off = meta_off + len(meta)
    data_off = seek_off + (-seek_off % align)
    regions = [(bank_off, len(bank)), (meta_off, len(meta)), (seek_off, 0),
               (0, 0), (data_off, len(blob))]
    head = b"WBND" + struct.pack("<II", *versions)
    for off, size in regions:
        head += struct.pack("<II", off, size)
    out = head + bank + meta
    return out + b"\0" * (data_off - len(out)) + blob


def read_xwb(data):
    """Parse a wave bank, for inspection and for tests."""
    if data[:4] != b"WBND":
        raise ConvertError("not a wave bank")
    version, header = struct.unpack_from("<II", data, 4)
    regions = [struct.unpack_from("<II", data, 12 + i * 8) for i in range(5)]
    flags, count, name = struct.unpack_from("<II64s", data, regions[0][0])
    sounds = []
    for i in range(count):
        dur, fmt, off, size = struct.unpack_from(
            "<IIII", data, regions[1][0] + i * 24)
        sounds.append(dict(
            samples=dur >> 4, codec=fmt & 3, channels=fmt >> 2 & 7,
            rate=fmt >> 5 & 0x3FFFF, block=(fmt >> 23 & 0xFF),
            offset=off, size=size))
    return dict(version=version, header=header, count=count,
                name=name.split(b"\0")[0].decode("ascii", "replace"),
                sounds=sounds)


def is_blank(image):
    """True if an image is one flat color, like an all-black background."""
    try:
        import numpy as np
        pixels = np.frombuffer(render_bgra(image, 32, 32), np.uint8)
    except Exception:
        return False
    return int(pixels.reshape(-1, 4)[:, :3].std(axis=0).max()) < 3


def find_art(song):
    """Pick the jacket: #JACKET, a *jacket* file, then background or banner."""
    folder = song.path.parent
    tagged = dict((tag, find_file(folder, name, []))
                  for tag, name in song.images)
    candidates = [tagged.get("JACKET")]
    candidates += sorted(p for p in folder.iterdir() if p.is_file()
                         and p.suffix.lower() in (".png", ".jpg", ".jpeg")
                         and re.search(r"jacket|(^|[-_ ])jk([-_ .]|$)",
                                       p.stem.lower()))
    candidates += [tagged.get("BACKGROUND"), tagged.get("BANNER")]
    candidates += sorted(p for p in folder.iterdir() if p.is_file()
                         and p.suffix.lower() in (".png", ".jpg", ".jpeg"))
    seen = set()
    for image in candidates:
        if image is None or image in seen:
            continue
        seen.add(image)
        if not is_blank(image):
            return image
    return None


def render_bgra(image, width, height):
    """Return BGRA bytes of the image, centre-cropped to the target shape."""
    exe = shutil.which("ffmpeg")
    if not exe:
        raise ConvertError("ffmpeg was not found on PATH")
    if image:
        source = ["-i", str(image)]
    else:
        source = ["-f", "lavfi", "-i", f"color=c=0x223355:s={width}x{height}"]
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
          f"crop={width}:{height}")
    run = subprocess.run(
        [exe, "-v", "error", *source, "-vf", vf, "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "bgra", "-"], capture_output=True)
    if run.returncode or len(run.stdout) != width * height * 4:
        raise ConvertError(f"could not render artwork: {run.stderr[:200]!r}")
    return run.stdout


def dds_header(width, height, dxt1):
    size = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 8
    flags = 0x81007 if dxt1 else 0x100F
    pitch = size if dxt1 else width * 4
    if dxt1:
        pixel = struct.pack("<II4s5I", 32, 4, b"DXT1", 0, 0, 0, 0, 0)
    else:
        pixel = struct.pack("<II4s5I", 32, 0x41, b"\0\0\0\0", 32, 0xFF0000,
                            0xFF00, 0xFF, 0xFF000000)
    return b"DDS " + struct.pack("<7I44x", 124, flags, height, width, pitch,
                                 0, 0) + pixel + struct.pack("<5I", 0x1000,
                                                             0, 0, 0, 0)


def rgb565(p):
    return (p[2] >> 3) << 11 | (p[1] >> 2) << 5 | p[0] >> 3


def dxt1_encode(bgra, width, height):
    """Simple DXT1 encoder: brightest pixel plus the pixel farthest from it."""
    out = bytearray()
    for by in range(0, height, 4):
        for bx in range(0, width, 4):
            px = [bgra[((by + y) * width + bx + x) * 4:
                       ((by + y) * width + bx + x) * 4 + 3]
                  for y in range(4) for x in range(4)]
            hi = max(px, key=lambda p: p[0] + 2 * p[1] + p[2])
            lo = max(px, key=lambda p: sum((a - b) ** 2
                                           for a, b in zip(p, hi)))
            c0, c1 = rgb565(hi), rgb565(lo)
            if c0 < c1:
                c0, c1, hi, lo = c1, c0, lo, hi
            bits = 0
            if c0 != c1:
                ramp = [tuple(hi), tuple(lo),
                        tuple((2 * h + l) // 3 for h, l in zip(hi, lo)),
                        tuple((h + 2 * l) // 3 for h, l in zip(hi, lo))]
                for i, p in enumerate(px):
                    best = min(range(4), key=lambda k: sum(
                        (a - b) ** 2 for a, b in zip(p, ramp[k])))
                    bits |= best << (i * 2)
            out += struct.pack("<HHI", c0, c1, bits)
    return bytes(out)


def make_dds(image, width, height, dxt1):
    bgra = render_bgra(image, width, height)
    if dxt1:
        return dds_header(width, height, True) + dxt1_encode(bgra, width,
                                                             height)
    return dds_header(width, height, False) + bgra


def add_to_thumbnails(game, thumbs):
    """Add {name: dds bytes} to the newest shared thumbnail archive."""
    folder = game / "data" / "arc" / "thumbnail"
    arcs = sorted(folder.glob("jacket_thumbnails_ja_*.arc"), key=lambda p: int(
        p.stem.rsplit("_", 1)[1]))
    if not arcs:
        warn("no thumbnail archives found; the game will use its default")
        return
    live, backup = arcs[-1], arcs[-1].with_name(arcs[-1].name + ".bak")
    if not backup.exists():
        shutil.copy2(live, backup)
    files, data = read_arc(backup)
    contents, stored = {}, {}
    for name, (off, size, full) in files.items():
        contents[name] = data[off:off + size]
        if size != full:
            stored[name] = (contents[name], full)
    contents.update(thumbs)
    ordered = {name: contents[name] for name in sorted(contents)}
    live.write_bytes(write_arc(ordered, stored))



# ------------------------------------------------- folder name labels
# Konami IFS image packs hold the song-wheel folder labels. Their manifest is
# binary XML (kbin); images are named by the MD5 of their texture name.
KBIN_SIX='0123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz'
# type id -> (struct fmt, count)
KBIN_T={2:('b',1),3:('B',1),4:('h',1),5:('H',1),6:('i',1),7:('I',1),8:('q',1),9:('Q',1),10:('B',-1),11:('s',-1),12:('4B',1),13:('I',1),14:('f',1),15:('d',1),
 16:('2b',1),17:('2B',1),18:('2h',1),19:('2H',1),20:('2i',1),21:('2I',1),22:('2q',1),23:('2Q',1),24:('2f',1),25:('2d',1),
 26:('3b',1),27:('3B',1),28:('3h',1),29:('3H',1),30:('3i',1),31:('3I',1),32:('3q',1),33:('3Q',1),34:('3f',1),35:('3d',1),
 36:('4b',1),37:('4B',1),38:('4h',1),39:('4H',1),40:('4i',1),41:('4I',1),42:('4q',1),43:('4Q',1),44:('4f',1),45:('4d',1),52:('b',1)}
KBIN_ENC={0x00:'cp932',0x20:'ascii',0x40:'latin1',0x60:'euc_jp',0x80:'cp932',0xA0:'utf8'}
class KNode:
    def __init__(s,name,typ): s.name,s.typ,s.attrs,s.children,s.value=name,typ,{},[],None
    def find(s,n): return next((c for c in s.children if c.name==n),None)
    def __repr__(s): return f'<{s.name} {s.attrs} {s.value!r} [{len(s.children)}]>'
def kbin_parse(b):
    assert b[0]==0xA0, b[:4]
    six=b[1]==0x42; enc=KBIN_ENC.get(b[2]&0xE0,'cp932')
    nl=struct.unpack_from('>I',b,4)[0]; nodes=b[8:8+nl]; p=8+nl
    dl=struct.unpack_from('>I',b,p)[0]; data=b[p+4:p+4+dl]
    np=0; dp=[0]; b4=[0]; b2=[0]; pos=[0]; base=p+4
    def name():
        nonlocal np
        if six:
            l=nodes[np]; np+=1; nb=(l*6+7)//8; bits=int.from_bytes(nodes[np:np+nb],'big'); np+=nb
            bits>>= nb*8-l*6
            return ''.join(KBIN_SIX[(bits>>(6*(l-1-i)))&63] for i in range(l))
        l=(nodes[np]&0xBF)+1; np+=1; s=nodes[np:np+l].decode(enc); np+=l; return s
    def align4():
        dp[0]=(dp[0]+3)&~3
    def rdata(n):
        # aligned read
        if n==1:
            if b4[0]%4==0: b4[0]=dp[0]; dp[0]+=4
            v=data[b4[0]:b4[0]+1]; b4[0]+=1; return v
        if n==2:
            if b2[0]%4==0: b2[0]=dp[0]; dp[0]+=4
            v=data[b2[0]:b2[0]+2]; b2[0]+=2; return v
        pos[0]=dp[0]; v=data[dp[0]:dp[0]+n]; dp[0]+=n; align4(); return v
    def rbuf():
        l=struct.unpack_from('>I',data,dp[0])[0]; dp[0]+=4; pos[0]=dp[0]; v=data[dp[0]:dp[0]+l]; dp[0]+=l; align4(); return v
    root=None; stack=[]
    while np<len(nodes):
        t=nodes[np]; np+=1; arr=bool(t&64); t&=63
        if t==0: continue
        if t==1:
            n=KNode(name(),'void')
            (stack[-1].children.append(n) if stack else None); root=root or n; stack.append(n); continue
        if t==46:
            k=name(); v=rbuf().rstrip(b'\0').decode(enc,'replace'); stack[-1].attrs[k]=v; continue
        if t==190 or t==63:
            break
        if t==62:
            stack.pop(); continue
        fmt,cnt=KBIN_T[t]; n=KNode(name(),t)
        if t==11:
            n.value=rbuf().rstrip(b'\0').decode(enc,'replace')
        elif t==10:
            n.value=rbuf()
        else:
            sz=struct.calcsize('>'+fmt)
            if arr: raw=rbuf(); n.value=list(struct.iter_unpack('>'+fmt,raw))
            else: raw=rdata(sz); n.value=struct.unpack('>'+fmt,raw)
        n.pos=base+pos[0]
        (stack[-1].children.append(n) if stack else None); root=root or n; stack.append(n)
    return root


# bemaniflag bit -> label texture (order of the folder table in gamemdx.dll)
FOLDER_TEXTURES = {1: "folder_bemani_2dx", 2: "folder_bemani_popn",
                   4: "folder_bemani_gitadora", 8: "folder_bemani_jubeat",
                   16: "folder_bemani_reflecbeat",
                   32: "folder_bemani_danceevolution",
                   64: "folder_bemani_soundvoltex",
                   128: "folder_bemani_miraidagakki",
                   256: "folder_bemani_beatstream",
                   1024: "folder_bemani_nostalgia",
                   2048: "folder_bemani_dancerush"}
LABEL_W, LABEL_H = 345, 23
LABEL_FONTS = ["C:/Windows/Fonts/ariblk.ttf", "C:/Windows/Fonts/arialbd.ttf",
               "/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def render_label(text):
    """Return 345x23 RGBA bytes: black text on transparent, like the game."""
    import numpy as np
    font = next((f for f in LABEL_FONTS if Path(f).exists()), None)
    if not font:
        raise ConvertError("no bold font found for the label")
    exe = shutil.which("ffmpeg")
    if not exe:
        raise ConvertError("ffmpeg was not found on PATH")
    font_arg = font.replace("\\", "/").replace(":", "\\:")
    with tempfile.TemporaryDirectory() as tmp:
        txt = Path(tmp) / "label.txt"
        txt.write_text(text, "utf-8")
        txt_arg = str(txt).replace("\\", "/").replace(":", "\\:")
        for size in range(16, 7, -1):
            draw = (f"drawtext=fontfile='{font_arg}':textfile='{txt_arg}':"
                    f"fontcolor=black:fontsize={size}:x=(w-text_w)/2:"
                    f"y=(h-text_h)/2")
            run = subprocess.run(
                [exe, "-v", "error", "-f", "lavfi", "-i",
                 f"color=c=black@0.0:s={LABEL_W}x{LABEL_H},format=rgba",
                 "-vf", draw, "-frames:v", "1", "-f", "rawvideo",
                 "-pix_fmt", "rgba", "-"], capture_output=True)
            if run.returncode or len(run.stdout) != LABEL_W * LABEL_H * 4:
                raise ConvertError("ffmpeg could not draw the label: "
                                   f"{run.stderr[:200]!r}")
            img = np.frombuffer(run.stdout, np.uint8).reshape(
                LABEL_H, LABEL_W, 4).copy()
            cols = np.nonzero(img[:, :, 3].max(axis=0))[0]
            if not len(cols) or (cols[0] >= 6 and cols[-1] <= LABEL_W - 7):
                break
        img[:, :, :3] = 0
        return img.tobytes()


def ifs_replace(ifs, name, pixels):
    """Swap one image in an IFS pack; offsets and checksums are updated."""
    mend = struct.unpack_from(">I", ifs, 16)[0]
    head = 0x24 if struct.unpack_from(">H", ifs, 4)[0] > 1 else 0x14
    man = bytearray(ifs[head:mend])
    root = kbin_parse(bytes(man))
    key = hashlib.md5(name.encode()).hexdigest()
    node = next((c for c in root.find("tex").children
                 if c.name.lstrip("_") == key), None)
    if node is None:
        raise ConvertError(f"{name} is not in this image pack")
    old = struct.unpack_from(">II", ifs, mend + node.value[0])
    if old[0] != len(pixels):
        raise ConvertError(f"{name} has an unexpected image size")
    packed = lz77_compress(pixels)
    blob = struct.pack(">II", len(pixels), len(packed)) + packed
    body = bytearray(ifs[mend:])
    body += b"\0" * (-len(body) % 16)
    struct.pack_into(">II", man, node.pos, len(body), len(blob))
    body += blob
    info = root.find("_info_")
    struct.pack_into(">I", man, info.find("size").pos, len(body))
    man[info.find("md5").pos:info.find("md5").pos + 16] = \
        hashlib.md5(bytes(body)).digest()
    out = bytearray(ifs[:head]) + man + body
    if head == 0x24:
        out[20:36] = hashlib.md5(bytes(man)).digest()
    return bytes(out)


def ifs_image(ifs, name):
    mend = struct.unpack_from(">I", ifs, 16)[0]
    head = 0x24 if struct.unpack_from(">H", ifs, 4)[0] > 1 else 0x14
    root = kbin_parse(ifs[head:mend])
    key = hashlib.md5(name.encode()).hexdigest()
    node = next(c for c in root.find("tex").children
                if c.name.lstrip("_") == key)
    off, size = node.value[0], node.value[1]
    full, stored = struct.unpack_from(">II", ifs, mend + off)
    raw = ifs[mend + off + 8:mend + off + 8 + stored]
    return raw if full == stored else lz77_decompress(raw)


def label_archives(game):
    return sorted((game / "data" / "arc" / "bm2d").glob(
        "select_music_card_lang_*_v*.arc"))


def set_folder_label(game, flag, text):
    """Draw a new name on a CS VERSION / BEMANI folder label."""
    return set_texture_label(game, FOLDER_TEXTURES[flag], text)


def set_texture_label(game, name, text):
    pixels = render_label(text)
    done = 0
    for arc in label_archives(game):
        backup = arc.with_name(arc.name + ".bak")
        if not backup.exists():
            shutil.copy2(arc, backup)
        live_files, live_data = read_arc(arc)
        inner = next(iter(live_files))
        ifs = arc_get(live_files, live_data, inner)
        try:
            ifs = ifs_replace(ifs, name, pixels)
        except ConvertError:
            continue
        packed = lz77_compress(ifs)
        arc.write_bytes(write_arc({inner: packed}, {inner: (packed, len(ifs))}))
        done += 1
    if not done:
        raise ConvertError("no folder label archives were found")
    return done


# ------------------------------------------------- DDR A3 folder patch
# Built against the omnimix gamemdx.dll (A20 PLUS 2022020200). AC VERSION
# folder 0x39 ("vr15") is registered but no series maps to it. The patch
# sends series 21 songs there and gives it its own label texture.
A3_SERIES = 21
A3_LABEL_TEXTURE = "folder_dummy"
WORLD_SERIES = 22
WORLD_LABEL_TEXTURE = "folder_hitchart_player2best30"


def _rel(src, dst):
    return struct.pack("<i", dst - (src + 5))


def _a3_edits(dll_data=None):
    """Return [(virtual address, original bytes, new bytes)].

    Folders 0x36-0x39 are rotated so the new A3 folder is first in the
    AC VERSION list: A3 0x36, A20 PLUS 0x37, A20 0x38, A 0x39.
    """
    rot = {0x36: 0x37, 0x37: 0x38, 0x38: 0x39, 0x39: 0x36}
    c1, c2, c3 = 0x1003FE48, 0x100E7F26, 0x1004EF61
    # series 21 -> folder 0x36 (A3), series 22 -> folder 0x35 (WORLD)
    pick = bytes.fromhex("83e814 83f801")
    cave1 = bytearray(pick + bytes.fromhex(
        "7713 f7d8 83c036 39442418 0f94442416"))
    cave1 += b"\xe9" + _rel(c1 + len(cave1), 0x10036B28)
    cave1 += b"\xe9" + _rel(c1 + len(cave1), c3)
    cave3 = bytearray(bytes.fromhex("83c014 83f812"))
    cave3 += b"\x0f\x87" + struct.pack(
        "<i", 0x10036B28 - (c3 + len(cave3) + 6))
    cave3 += b"\xe9" + _rel(c3 + len(cave3), 0x10036B8A)
    cave2 = bytearray(pick + bytes.fromhex(
        "7711 f7d8 83c036 6a00 8d5584 52 50"))
    cave2 += b"\xe9" + _rel(c2 + len(cave2), 0x1008AF4E)
    cave2 += bytes.fromhex("83c014 83f812")
    cave2 += b"\x0f\x87" + struct.pack(
        "<i", 0x1008AF5A - (c2 + len(cave2) + 6))
    cave2 += b"\xe9" + _rel(c2 + len(cave2), 0x1008AE07)
    old_table = struct.pack("<8I", 0x70327276, 0x101D6488, 0x32417276,
                            0x101D6470, 0x31417276, 0x101D645C,
                            0x35317276, 0x101D6448)
    new_table = struct.pack("<8I", 0x35317276, 0x101D5E3C, 0x70327276,
                            0x101D6488, 0x32417276, 0x101D6470,
                            0x31417276, 0x101D645C)
    edits = [
        (0x10036B85, bytes.fromhex("83f812779e"), b"\xe9" + _rel(0x10036B85, c1)),
        (0x1008ADFE, bytes.fromhex("83f8120f8753010000"),
         b"\xe9" + _rel(0x1008ADFE, c2) + b"\x90" * 4),
        (0x101C9868, old_table, new_table),
        (c1, b"\xcc" * len(cave1), bytes(cave1)),
        (c2, b"\xcc" * len(cave2), bytes(cave2)),
        (c3, b"\xcc" * len(cave3), bytes(cave3)),
        # hit chart category loses folder 0x35, AC VERSION starts there
        (0x1008A1D3, b"\x07", b"\x06"),
        (0x1008A1E8, b"\x36", b"\x35"),
        (0x1008A1F3, b"\x13", b"\x14"),
        (0x10051657, b"\x36", b"\x35"),
        (0x1005167A, b"\x36", b"\x35"),
        (0x101C9860, struct.pack("<I", 0x32506368),
         struct.pack("<I", 0x44577276)),
    ]
    # series -> folder immediates in both switches
    for va in (0x10036C73, 0x10036C82, 0x10036C91,
               0x1008AE23, 0x1008AF31, 0x1008AF42, 0x1008AF4D):
        edits.append((va, None, rot))
    # id -> folder table used to remember folders
    for i in range(0x77):
        edits.append((0x101CA410 + i * 4, "table", rot))
    if dll_data is None:
        return edits
    out = []
    for va, old, new in edits:
        if isinstance(new, dict):
            off = _pe_offset(dll_data, va)
            if old == "table":
                value = struct.unpack_from("<I", dll_data, off)[0]
                if value not in new:
                    continue
                out.append((va, struct.pack("<I", value),
                            struct.pack("<I", new[value])))
            else:
                value = dll_data[off]
                out.append((va, bytes([value]), bytes([new[value]])))
        else:
            out.append((va, old, new))
    return out


def _pe_offset(data, va):
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    count = struct.unpack_from("<H", data, pe + 6)[0]
    opt = struct.unpack_from("<H", data, pe + 20)[0]
    base = struct.unpack_from("<I", data, pe + 52)[0]
    for i in range(count):
        q = pe + 24 + opt + i * 40
        vsize, rva, rsize, raw = struct.unpack_from("<IIII", data, q + 8)
        if rva <= va - base < rva + max(vsize, rsize):
            return raw + va - base - rva
    raise ConvertError("address outside the DLL")


def _is_original(data):
    for va, old, new in _a3_edits(data):
        off = _pe_offset(data, va)
        if data[off:off + len(old)] != old:
            return False
    return True


def a3_patch_state(dll):
    """original, patched, or unknown (another build or an older patch)."""
    data = Path(dll).read_bytes()
    if _is_original(data):
        return "original"
    backup = Path(dll).with_name("gamemdx.dll.a3bak")
    if backup.exists():
        base = backup.read_bytes()
        if _is_original(base) and _apply_a3(base) == data:
            return "patched"
        if _is_original(base):
            return "outdated"
    return "unknown"


def _apply_a3(base):
    data = bytearray(base)
    for va, old, new in _a3_edits(base):
        off = _pe_offset(data, va)
        data[off:off + len(old)] = new
    return bytes(data)


def set_a3_patch(game, enable):
    """Patch or restore gamemdx.dll; the original is kept as .a3bak."""
    dll = game / "gamemdx.dll"
    if not dll.exists():
        raise ConvertError("gamemdx.dll is not in the game folder")
    backup = dll.with_name("gamemdx.dll.a3bak")
    state = a3_patch_state(dll)
    if state == "unknown":
        raise ConvertError("this gamemdx.dll is a different version; "
                           "the A3 patch only fits the omnimix DLL")
    if state == "original" and not backup.exists():
        shutil.copy2(dll, backup)
    base = dll.read_bytes() if state == "original" else backup.read_bytes()
    dll.write_bytes(_apply_a3(base) if enable else base)
    if enable:
        set_texture_label(game, A3_LABEL_TEXTURE, "DDR A3")
        set_texture_label(game, WORLD_LABEL_TEXTURE, "DDR WORLD")
    return "patched" if enable else "original"


# ---------------------------------------------------------------- pipeline


def find_songs(root):
    root = Path(root)
    found = {}
    for f in sorted(list(root.rglob("*.sm")) + list(root.rglob("*.ssc"))):
        current = found.get(f.parent)
        if current is None or f.suffix.lower() == ".ssc":
            found[f.parent] = f
    return list(found.values())


def make_basename(song, length, used):
    seed = f"{song.title}|{song.artist}"
    for salt in range(1000):
        digest = hashlib.sha1(f"{seed}|{salt}".encode()).hexdigest()
        name = "c" + digest[:max(length, 2) - 1]
        if name not in used:
            used.add(name)
            return name
    raise ConvertError("could not create a unique basename")


def detect_audio_dir(game):
    """Find the folder holding paired stock .xwb/.xsb song banks."""
    known = game / "data" / LAYOUT_AUDIO
    if any(known.glob("*.xsb")):
        return known
    best = {}
    for xwb in (game / "data").rglob("*.xwb"):
        if xwb.with_suffix(".xsb").exists():
            best[xwb.parent] = best.get(xwb.parent, 0) + 1
    return max(best, key=best.get) if best else None


def find_stock_ssq(game, base):
    """Find a stock song's chart, either <base>.ssq or <base>/<any>.ssq."""
    data = game / "data"
    hits = sorted(data.rglob(f"{base}.ssq"))
    if hits:
        return hits[0]
    return next((p for p in sorted(data.rglob("*.ssq"))
                 if base in p.relative_to(data).parts[:-1]), None)


def renamed_rel(path, data, old, new):
    parts = list(path.relative_to(data).parts)
    parts[-2:] = [part.replace(old, new) for part in parts[-2:]]
    return Path(*parts)


def xsb_checksum(data):
    """CRC-16/X-25 of everything after the 18-byte header."""
    crc = 0xFFFF
    for byte in data[18:]:
        crc ^= byte
        for _ in range(8):
            crc = crc >> 1 ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def build_xsb(template, old, new):
    """Clone a stock two-cue sound bank for a same-length basename."""
    if len(old) != len(new):
        raise ConvertError("XSB template needs a same-length basename")
    data = bytearray(template.replace(old.encode(), new.encode()))
    table, entries = struct.unpack_from("<II", data, 0x3E)[0], \
        struct.unpack_from("<I", data, 0x42)[0]
    # The cue hash formula is unknown, so make every bucket reach both cues.
    for slot in range((entries - table) // 2):
        struct.pack_into("<H", data, table + slot * 2, 0)
    struct.pack_into("<H", data, entries + 4, 1)
    struct.pack_into("<H", data, entries + 10, 0xFFFF)
    struct.pack_into("<H", data, 8, xsb_checksum(data))
    return bytes(data)


def preview_first(xwb_head):
    """True if a stock bank stores its preview before the full song."""
    info = read_xwb(xwb_head)
    durations = [s["samples"] for s in info["sounds"]]
    return len(durations) == 2 and durations[0] < durations[1]


def process(args, game=None):
    songs = find_songs(args.songs)
    if game and not (game / "data").is_dir():
        raise SystemExit(f"{game} has no data folder")
    direct = game is not None and args.target == "a3"
    if direct and not args.template_song:
        raise SystemExit("A3 installs need --template-song")
    if not songs:
        raise SystemExit(f"no .sm or .ssc files under {args.songs}")
    db = load_musicdb(game) if game else None
    entries_db = list(db[0]) if db else []
    used = {e.findtext("basename") for e in entries_db}
    mcodes = {int(e.findtext("mcode", "0")) for e in entries_db}
    chart_flags = collections.Counter(
        flag for e in entries_db
        for flag in (e.findtext("limited_ary") or "").split() if flag != "-1")
    available = chart_flags.most_common(1)[0][0] if chart_flags else "0"
    if entries_db and not chart_flags:
        available = None
    template_entry, tpl_base = None, args.template_song
    if tpl_base:
        template_entry = next(
            (e for e in entries_db if e.findtext("basename") == tpl_base), None)
        if template_entry is None:
            raise SystemExit(f"template song {tpl_base} is not in musicdb")
    stock_ssq = None
    audio_dir = detect_audio_dir(game) if game else None
    xsb_template = None
    swap_order = False
    xwb_versions = (43, 42)  # startup.arc's ddr.xgs is XACT version 43
    if audio_dir:
        with next(audio_dir.glob("*.xwb")).open("rb") as stock:
            xwb_versions = struct.unpack("<II", stock.read(12)[4:12])
    if game and tpl_base:
        stock_ssq = find_stock_ssq(game, tpl_base)
        if not stock_ssq and direct:
            raise SystemExit(f"no stock chart found for {tpl_base}")
        if audio_dir and (audio_dir / f"{tpl_base}.xsb").exists():
            xsb_template = (audio_dir / f"{tpl_base}.xsb").read_bytes()
            with (audio_dir / f"{tpl_base}.xwb").open("rb") as stock:
                swap_order = preview_first(stock.read(1024))
        elif direct:
            raise SystemExit(f"no stock audio found for {tpl_base}")

    if direct:
        out = game / "data"
    elif game:
        out = game / "data_mods" / args.pack
    else:
        out = Path(args.out)
    ssq_dir = out / LAYOUT_SSQ
    audio_rel = audio_dir.relative_to(game / "data") if audio_dir \
        else LAYOUT_AUDIO
    audio_out = out / audio_rel
    jacket_out = out / "arc" / "jacket"
    thumb_store = (game / "sm2ddr_thumbnails") if game else out / "thumbnails"
    frag_path = out / "gamedata" / "musicdb.merged.xml"
    if db:
        rel = Path(db[1])
        rel = rel.relative_to("data") if rel.parts[0] == "data" else rel
        frag_path = out / rel.with_suffix(".merged.xml")
    if direct:
        frag_path = game / "sm2ddr_songs.xml"
    enc, declared = (db[2], db[3]) if db else ("utf-8", "UTF-8")

    previous = {}
    if frag_path.exists():
        text, _, _ = decode_xml(frag_path.read_bytes())
        previous = {e.findtext("basename"): e for e in ET.fromstring(text)}
        used |= set(previous)
        mcodes |= {int(e.findtext("mcode", "0")) for e in previous.values()}
    if args.mcode:
        next_mcode = max([args.mcode - 1] + list(mcodes)) + 1
    else:
        next_mcode = max(mcodes) + 1 if mcodes else 39000
    length = len(tpl_base) if tpl_base else 4
    failed = 0
    for path in songs:
        print(f"{path.parent.name}")
        try:
            song = parse_simfile(path)
            ssq, levels, shift = convert_charts(song, args)
            music = find_file(path.parent, song.music,
                              ["*.ogg", "*.mp3", "*.wav", "*.flac"])
            if not music and not args.no_audio:
                raise ConvertError("audio file not found")
            full_title = make_entry(None, 0, "", song, levels, 0) \
                .findtext("title")
            base = next(
                (b for b, e in previous.items()
                 if e.findtext("title") == full_title and
                 e.findtext("artist") == song.artist), None)
            if base is None:
                base = make_basename(song, length, used)
                mcode = next_mcode
                next_mcode += 1
            else:
                mcode = int(previous[base].findtext("mcode"))
            ssq_path = ssq_dir / f"{base}.ssq"
            if direct:
                ssq_path = out / renamed_rel(stock_ssq, out, tpl_base, base)
            ssq_path.parent.mkdir(parents=True, exist_ok=True)
            ssq_path.write_bytes(ssq)
            if not args.no_audio:
                audio_out.mkdir(parents=True, exist_ok=True)
                start = song.sample_start if song.sample_start >= 0 else 30
                sounds = [
                    adpcm_encode(decode_audio(music, shift)),
                    adpcm_encode(decode_audio(music, 0, start,
                                              song.sample_length, fade=True))]
                if swap_order:
                    sounds.reverse()
                (audio_out / f"{base}.xwb").write_bytes(
                    build_xwb(base, sounds, xwb_versions))
                if xsb_template:
                    (audio_out / f"{base}.xsb").write_bytes(
                        build_xsb(xsb_template, tpl_base, base))
                art = find_art(song)
                if not art:
                    warn("no artwork found; using a plain jacket")
                jacket_out.mkdir(parents=True, exist_ok=True)
                (jacket_out / f"{base}_jk.arc").write_bytes(write_arc(
                    {f"data/jacket/{base}_jk.dds":
                     make_dds(art, 512, 512, False)}))
                thumb_store.mkdir(parents=True, exist_ok=True)
                (thumb_store / f"{base}_tn.dds").write_bytes(
                    make_dds(art, 192, 192, True))
            previous[base] = make_entry(template_entry, mcode, base, song,
                                        levels, args.series, available,
                                        args.bemaniflag)
            print(f"  -> {base} (mcode {mcode})")
        except ConvertError as e:
            failed += 1
            print(f"  skipped: {e}", file=sys.stderr)
    if previous:
        write_fragment(frag_path, list(previous.values()), enc, declared)
        if direct:
            total = patch_startup(game, list(previous.values()))
            add_to_thumbnails(game, {
                f"data/jacket/thumbnail/{p.name}": p.read_bytes()
                for p in sorted(thumb_store.glob("*_tn.dds"))})
            print(f"database updated: {total} songs "
                  f"(original kept with a .bak extension)")
    print(f"\n{len(songs) - failed} of {len(songs)} songs written to {out}")
    if warnings_seen:
        print(f"{len(warnings_seen)} warnings")
    return 1 if failed else 0


def hidden_songs(game):
    """Return basenames of stock songs taken out of the song list."""
    path = game / "sm2ddr_hidden.json"
    return set(json.loads(path.read_text("utf-8"))) if path.exists() else set()


def course_songs(game):
    """Return mcodes that courses use. Hiding those could break a course."""
    found = database_file(game)
    if not found or not found[1].exists():
        return set()
    files, data = read_arc(found[1])
    name = next((n for n in files if n.endswith("coursedb.xml")), None)
    if not name:
        return set()
    text = arc_get(files, data, name).decode("utf-8", "replace")
    return set(re.findall(r"<music>\s*<mcode[^>]*>(\d+)", text))


def set_hidden(game, hide=(), show=()):
    """Take songs off the song list or put them back. Files are kept."""
    hidden = (hidden_songs(game) | set(hide)) - set(show)
    (game / "sm2ddr_hidden.json").write_text(
        json.dumps(sorted(hidden)), "utf-8")
    rebuild_game_lists(game, tracked_songs(game))


def deleted_songs(game):
    """Return basenames whose data was permanently deleted."""
    path = game / "sm2ddr_deleted.json"
    return set(json.loads(path.read_text("utf-8"))) if path.exists() else set()


def song_files(game, base):
    """Return every file that belongs to a song."""
    data = game / "data"
    chart_dir, sound = data / LAYOUT_SSQ, data / LAYOUT_AUDIO
    paths = [chart_dir / f"{base}.ssq", chart_dir / f"{base}_cp.ssq"]
    paths += [chart_dir / f"{base}_{n}.ssq" for n in range(1, 10)]
    for stem in (base, f"{base}_c"):
        paths += [sound / f"{stem}{ext}" for ext in (".xsb", ".xwb", ".xgs")]
    paths += [data / "arc" / "jacket" / f"{base}_jk.arc",
              game / "sm2ddr_thumbnails" / f"{base}_tn.dds"]
    return [p for p in paths if p.exists()]


def tracked_songs(game):
    """Return custom song entries recorded by earlier installs."""
    path = game / "sm2ddr_songs.xml"
    if not path.exists():
        return []
    return list(ET.fromstring(decode_xml(path.read_bytes())[0]))


def rebuild_game_lists(game, entries):
    """Rewrite the tracking file, startup.arc and thumbnails from entries."""
    db = load_musicdb(game)
    write_fragment(game / "sm2ddr_songs.xml", entries, db[2], db[3])
    patch_startup(game, entries)
    store = game / "sm2ddr_thumbnails"
    add_to_thumbnails(game, {
        f"data/jacket/thumbnail/{p.name}": p.read_bytes()
        for p in sorted(store.glob("*_tn.dds"))})


def delete_song(game, base):
    """Permanently delete a song's data and take it off the song list."""
    custom = tracked_songs(game)
    keep = [e for e in custom if e.findtext("basename") != base]
    db = load_musicdb(game)
    stock = next((e for e in db[0] if e.findtext("basename") == base), None)
    if stock is None and len(keep) == len(custom):
        raise ConvertError(f"{base} was not found")
    if stock is not None and stock.findtext("mcode") in course_songs(game):
        raise ConvertError("that song is used in a course")
    for path in song_files(game, base):
        path.unlink()
    hidden = hidden_songs(game)
    if stock is not None:
        hidden.add(base)
        (game / "sm2ddr_deleted.json").write_text(
            json.dumps(sorted(deleted_songs(game) | {base})), "utf-8")
    else:
        hidden.discard(base)
    (game / "sm2ddr_hidden.json").write_text(
        json.dumps(sorted(hidden)), "utf-8")
    rebuild_game_lists(game, keep)


# ------------------------------------------- import from a DDR WORLD install
# World numbers its versions one lower than the A3/WORLD patch does.
WORLD_SERIES = {19: 19, 20: 21, 21: 22}
WORLD_KEEP = ("mcode", "basename", "title", "title_yomi", "artist", "bpmmin",
              "bpmmax", "series", "diffLv")


def same_title(text):
    return re.sub(r"\W", "", unicodedata.normalize("NFKC", text or "").lower())


def ssq_bpms(data):
    """Return (lowest, highest) BPM from a chart's tempo chunk."""
    for c in read_ssq(data):
        if c["kind"] != 1:
            continue
        tps = c["p2"] or DEFAULT_TPS
        rates = []
        pairs = list(zip(c["offsets"], c["ticks"]))
        for (o1, t1), (o2, t2) in zip(pairs, pairs[1:]):
            if o2 > o1 and t2 > t1:
                rates.append(round((o2 - o1) / 1024 * 60 * tps / (t2 - t1)))
        if rates:
            return min(rates), max(rates)
    return None


def world_root(path):
    """Accept the install folder, its contents folder or its data folder."""
    path = Path(path)
    for root in (path, path / "contents", path.parent):
        if (root / "data" / "arc" / "startup.arc").exists():
            return root
    raise ConvertError("That folder has no data\\arc\\startup.arc.")


def world_songs(game, world):
    """Return (songs to import, songs already in the game by title).

    Each item is (world entry, tracked entry with the same title or None).
    """
    world = world_root(world)
    files, data = read_arc(world / "data" / "arc" / "startup.arc")
    name = next(n for n in files if n.endswith("musicdb.xml"))
    root = ET.fromstring(decode_xml(arc_get(files, data, name))[0])
    db = load_musicdb(game)
    stock = {e.findtext("basename") for e in db[0]}
    stock_titles = {same_title(e.findtext("title")) for e in db[0]}
    tracked = tracked_songs(game)
    by_base = {e.findtext("basename"): e for e in tracked}
    by_title = {same_title(e.findtext("title")): e for e in tracked}
    found = []
    for entry in root:
        base = entry.findtext("basename")
        series = int(entry.findtext("series", "0"))
        if base in stock or series not in WORLD_SERIES:
            continue
        title = same_title(entry.findtext("title"))
        if title in stock_titles:
            continue
        found.append((entry, by_base.get(base) or by_title.get(title)))
    return world, found


def import_world(game, world, replace=False):
    """Copy DDR A3 and WORLD songs from a World install into the omnimix.

    Songs whose title matches a song you converted are skipped, or with
    replace=True your converted copy is deleted and the official one used.
    """
    game = Path(game)
    state = a3_patch_state(game / "gamemdx.dll")
    if state != "patched":
        raise ConvertError("Turn on the DDR WORLD and A3 folders first.")
    world, found = world_songs(game, world)
    src = world / "data"
    thumbs = {}
    for arc in sorted((src / "arc" / "thumbnail").glob(
            "jacket_thumbnails_*.arc")):
        files, data = read_arc(arc)
        for n in files:
            thumbs.setdefault(Path(n).name, (arc, n))
    entries = tracked_songs(game)
    db = load_musicdb(game)
    mcodes = {int(e.findtext("mcode")) for e in list(db[0]) + entries}
    next_mcode = max(mcodes) + 1
    out = game / "data"
    store = game / "sm2ddr_thumbnails"
    store.mkdir(exist_ok=True)
    added = skipped = 0
    opened = {}
    for entry, mine in found:
        base, title = entry.findtext("basename"), entry.findtext("title")
        if mine is not None and not replace:
            skipped += 1
            continue
        chart = src / LAYOUT_SSQ / f"{base}.ssq"
        audio = [src / LAYOUT_AUDIO / f"{base}{ext}" for ext in (".xsb", ".xwb")]
        jacket = src / "arc" / "jacket" / f"{base}_jk.arc"
        missing = [p.name for p in [chart, *audio] if not p.exists()]
        if missing:
            print(f"{title}: skipped, missing {', '.join(missing)}")
            continue
        if mine is not None:
            old = mine.findtext("basename")
            if old != base:
                for path in song_files(game, old):
                    path.unlink()
            entries = [e for e in entries if e.findtext("basename") != old]
            mcodes.discard(int(mine.findtext("mcode")))
        charts = [chart] + sorted(
            p for p in (src / LAYOUT_SSQ).glob(f"{base}_*.ssq")
            if re.fullmatch(rf"{re.escape(base)}_(\d|cp)", p.stem))
        for path in charts:
            shutil.copyfile(path, out / LAYOUT_SSQ / path.name)
        for path in audio:
            shutil.copyfile(path, out / LAYOUT_AUDIO / path.name)
        if jacket.exists():
            shutil.copyfile(jacket, out / "arc" / "jacket" / jacket.name)
        thumb = thumbs.get(f"{base}_tn.dds")
        if thumb:
            if thumb[0] not in opened:
                opened[thumb[0]] = read_arc(thumb[0])
            files, data = opened[thumb[0]]
            (store / f"{base}_tn.dds").write_bytes(
                arc_get(files, data, thumb[1]))
        new = ET.Element("music")
        for tag in WORLD_KEEP:
            el = entry.find(tag)
            if el is not None:
                copy = ET.SubElement(new, tag, dict(el.attrib))
                copy.text = el.text
        new.find("series").text = str(
            WORLD_SERIES[int(entry.findtext("series"))])
        mcode = int(entry.findtext("mcode"))
        if mcode in mcodes:
            mcode, next_mcode = next_mcode, next_mcode + 1
        mcodes.add(mcode)
        new.find("mcode").text = str(mcode)
        if new.find("title_yomi") is None:
            set_field(new, "title_yomi", sort_key(title))
        if new.find("artist") is None:
            set_field(new, "artist", "")
        if new.find("bpmmax") is None:
            bpm = ssq_bpms(chart.read_bytes()) or (150, 150)
            if bpm[0] != bpm[1]:
                set_field(new, "bpmmin", bpm[0], "u16")
            set_field(new, "bpmmax", bpm[1], "u16")
        order = {t: i for i, t in enumerate(WORLD_KEEP)}
        new[:] = sorted(new, key=lambda el: order.get(el.tag, 99))
        entries.append(new)
        added += 1
        print(f"{title}")
    if added:
        rebuild_game_lists(game, entries)
    print(f"Imported {added} songs." + (
        f" Skipped {skipped} you already added; tick Replace to use the "
        "official versions instead." if skipped else ""))
    return added, skipped


def cmd_world(args):
    import_world(args.game, args.world, args.replace)
    return 0


def cmd_inspect(args):
    game = Path(args.game)
    data = game / "data"
    print(f"data folder: {'found' if data.exists() else 'MISSING'}")
    ssqs = sorted(data.rglob("*.ssq"))
    print(f"charts: {len(ssqs)} .ssq files, for example "
          f"{ssqs[0].relative_to(data) if ssqs else 'none'}")
    print(f"data_mods: {'found' if (game / 'data_mods').exists() else 'none'}")
    audio = detect_audio_dir(game)
    print(f"audio bank folder: {audio}")
    if audio:
        bank = next(audio.glob("*.xwb"))
        info = read_xwb(bank.read_bytes())
        print(f"sample bank {bank.name}: {info}")
    db = load_musicdb(game)
    if not db:
        print("musicdb: not found")
        return 0
    root, where, _, declared = db
    print(f"musicdb: {where} ({len(root)} songs, {declared})")
    if len(root):
        sample = root[-1]
        print("sample entry:")
        print(ET.tostring(sample, encoding="unicode"))
        name = sample.findtext("basename")
        hits = [p for p in data.rglob(f"*{name}*") if p.is_file()][:20]
        print(f"files for {name}:")
        for p in hits:
            print(f"  {p.relative_to(game)}  {p.stat().st_size}")
            if p.suffix == ".arc":
                for inner, (_, stored, full) in read_arc(p)[0].items():
                    print(f"    {inner}  {full} (stored {stored})")
    return 0


def cmd_dump(args):
    for c in read_ssq(Path(args.file).read_bytes()):
        line = (f"type {c['kind']} p2=0x{c['p2']:04x} count={c['count']} "
                f"p4={c['p4']} size={c['size']}")
        print(line)
        if c["kind"] == 2:
            print("  " + " ".join(f"{o}:{v:04x}" for o, v in
                                  zip(c["offsets"], c["codes"])))
        if c["kind"] == 1:
            print("  " + " ".join(f"{o}:{t}" for o, t in
                                  zip(c["offsets"], c["ticks"])))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("import-world",
                       help="copy DDR A3 and WORLD songs from a World install")
    w.add_argument("world")
    w.add_argument("--game", required=True)
    w.add_argument("--replace", action="store_true",
                   help="swap songs you converted for the official versions")
    i = sub.add_parser("inspect")
    i.add_argument("game")
    d = sub.add_parser("dump-ssq")
    d.add_argument("file")
    for name in ("build", "install"):
        s = sub.add_parser(name)
        s.add_argument("songs")
        if name == "build":
            s.add_argument("--out", required=True)
        else:
            s.add_argument("--game", required=True)
            s.add_argument("--pack", default="custom_songs")
            s.add_argument("--target", choices=("world", "a3"),
                           default="world",
                           help="a3 writes into data/ and edits startup.arc")
        s.add_argument("--template-song",
                       help="stock basename whose file layout and series to follow")
        s.add_argument("--mcode", type=int)
        s.add_argument("--series", type=int)
        s.add_argument("--bemaniflag", type=int,
                       help="set this flag, as omnimix additions do (4)")
        s.add_argument("--tps", type=int, default=DEFAULT_TPS)
        s.add_argument("--no-audio", action="store_true")
    args = p.parse_args(argv)
    if args.cmd == "inspect":
        return cmd_inspect(args)
    if args.cmd == "import-world":
        return cmd_world(args)
    if args.cmd == "dump-ssq":
        return cmd_dump(args)
    return process(args, Path(args.game) if args.cmd == "install" else None)


if __name__ == "__main__":
    sys.exit(main())

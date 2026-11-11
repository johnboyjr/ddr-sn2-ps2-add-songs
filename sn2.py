"""Replace a song in Dance Dance Revolution SuperNOVA 2 (PS2, SLUS-21608).

Works on an extracted copy of the disc (the folder holding SYSTEM.CNF) and
writes a new, bootable .iso. The original folder is never changed.

    python sn2.py replace GAME_FOLDER SONG_FOLDER --slot csto --out new.iso

What gets replaced for the slot's song:
  - its chart block in mdb/csq_all.bin (IMAGE.DAT)
  - its title label and banner in mdb/music_dt.bin (IMAGE.DAT)
  - MUSIC/<slot>.vig and SEL/<slot>_s.vig (MDB_SN1.DAT)
  - its BPM and difficulty levels in the game program
Files that grow are moved to the end of their .DAT, and the file table in the
game program is updated to match.
"""
import argparse
import math
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sm2ddr  # noqa: E402

SECTOR = 2048
ARCHIVES = ["SYSTEM.DAT", "IMAGE.DAT", "SOUND.DAT", "MDB_SN1.DAT"]
RECORD = 0xA8
TPS = 150


class SN2Error(Exception):
    pass


# ------------------------------------------------------------- compression
def lz_decompress(src):
    """Konami (KCET) LZ used by SuperNOVA 2.

    A flag byte covers the next 8 items, low bit first: 0 = literal byte,
    1 = command. Commands: 0xFF ends the stream; 0xC0-0xFE is a run of
    (b & 0x3F) + 8 literal bytes; 0x80-0xBF copies ((b >> 4) & 3) + 2 bytes
    from distance (b & 15) + 1; otherwise bytes a, b copy (a >> 2) + 3 bytes
    from distance ((a & 3) << 8) | b.
    """
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        flags = src[i]
        i += 1
        for bit in range(8):
            if i >= n:
                return bytes(out)
            if not flags >> bit & 1:
                out.append(src[i])
                i += 1
                continue
            a = src[i]
            if a == 0xFF:
                return bytes(out)
            if a >= 0xC0:
                k = (a & 0x3F) + 8
                out += src[i + 1:i + 1 + k]
                i += 1 + k
                continue
            if a & 0x80:
                length, dist = ((a >> 4) & 3) + 2, (a & 15) + 1
                i += 1
            else:
                length, dist = (a >> 2) + 3, ((a & 3) << 8) | src[i + 1]
                i += 2
            for _ in range(length):
                out.append(out[-dist] if dist <= len(out) else 0)
    return bytes(out)


def lz_compress(data):
    """Greedy compressor producing streams lz_decompress reads."""
    data = bytes(data)
    n = len(data)
    chains = {}
    tokens = []
    i = 0
    while i < n:
        best_len = best_dist = 0
        if i + 2 < n:
            for j in reversed(chains.get(data[i:i + 3], [])[-64:]):
                dist = i - j
                if dist > 1023:
                    break
                length = 3
                while length < 34 and i + length < n and \
                        data[j + length] == data[i + length]:
                    length += 1
                if length > best_len:
                    best_len, best_dist = length, dist
                    if length == 34:
                        break
        # a short copy: 2-5 bytes from 1-16 back, one byte instead of two
        short_len = 0
        for dist in range(1, min(16, i) + 1):
            length = 0
            while length < 5 and i + length < n and \
                    data[i + length - dist] == data[i + length]:
                length += 1
            if length >= 2 and length > short_len:
                short_len, short_dist = length, dist
        if short_len >= 2 and short_len >= best_len - 1 and \
                (best_len < 3 or short_len >= best_len):
            tokens.append(bytes((0x80 | (short_len - 2) << 4 | (short_dist - 1),)))
            step = short_len
        elif best_len >= 3:
            tokens.append(bytes(((best_len - 3) << 2 | best_dist >> 8,
                                 best_dist & 0xFF)))
            step = best_len
        else:
            tokens.append(data[i])
            step = 1
        for k in range(i, min(i + step, n - 2)):
            chains.setdefault(data[k:k + 3], []).append(k)
        i += step
    tokens.append(b"\xff")
    out = bytearray()
    for g in range(0, len(tokens), 8):
        group = tokens[g:g + 8]
        flags = sum(1 << b for b, t in enumerate(group) if isinstance(t, bytes))
        out.append(flags)
        for t in group:
            out += t if isinstance(t, bytes) else bytes((t,))
    return bytes(out)


# ------------------------------------------------------------- disc folder
def iso_listing(path):
    """Map 'DIR/NAME' (no ';1') to (lba, size) for every file on an ISO."""
    files = {}
    with open(path, "rb") as f:
        def sec(n, count=1):
            f.seek(n * SECTOR)
            return f.read(count * SECTOR)
        pvd = sec(16)
        if pvd[1:6] != b"CD001":
            raise SN2Error("That file is not a disc image (.iso).")
        total = struct.unpack_from("<I", pvd, 80)[0]

        def walk(lba, size, prefix):
            data = sec(lba, max(1, math.ceil(size / SECTOR)))
            i = 0
            while i < len(data):
                n = data[i]
                if n == 0:
                    i = (i // SECTOR + 1) * SECTOR
                    continue
                name = data[i + 33:i + 33 + data[i + 32]]
                at = struct.unpack_from("<I", data, i + 2)[0]
                length = struct.unpack_from("<I", data, i + 10)[0]
                if name not in (b"\0", b"\1"):
                    text = name.decode("latin1").split(";")[0]
                    rel = f"{prefix}{text}"
                    if data[i + 25] & 2:
                        walk(at, length, rel + "/")
                    else:
                        files[rel.upper()] = (at, length)
                i += n
        root = pvd[156:190]
        walk(struct.unpack_from("<I", root, 2)[0],
             struct.unpack_from("<I", root, 10)[0], "")
    return files, total


class Game:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.iso = None
        if self.folder.is_file():
            self.iso = self.folder
            self.files, self.total = iso_listing(self.iso)
            with open(self.iso, "rb") as f:
                def get(name):
                    lba, size = self.files[name.upper()]
                    f.seek(lba * SECTOR)
                    return f.read(size)
                cnf = get("SYSTEM.CNF").decode("latin1")
                m = re.search(r"cdrom0:\\([^;\s]+)", cnf)
                if not m:
                    raise SN2Error("SYSTEM.CNF has no BOOT2 line.")
                self.elf_name = m.group(1)
                self.elf = bytearray(get(self.elf_name))
            self._finish()
            return
        cnf = (self.folder / "SYSTEM.CNF").read_text("latin1")
        m = re.search(r"cdrom0:\\([^;\s]+)", cnf)
        if not m:
            raise SN2Error("SYSTEM.CNF has no BOOT2 line.")
        self.elf_name = m.group(1)
        self.elf = bytearray((self.folder / self.elf_name).read_bytes())
        self._finish()

    def _finish(self):
        if self.elf_name.upper() != "SLUS_216.08":
            raise SN2Error(f"{self.elf_name} is not DDR SuperNOVA 2 (US).")
        self._segments()
        self.toc = self._read_toc()
        self.records = {m.group(1).decode(): m.start() for m in re.finditer(
            rb"([a-z0-9]{4}[a-z0-9\x00])\x00\x05", self.elf)}
        self.records = {k.rstrip("\0"): v for k, v in self.records.items()
                        if struct.unpack_from("<H", self.elf, v + 8)[0] < 256}

    def _segments(self):
        e = self.elf
        phoff = struct.unpack_from("<I", e, 0x1C)[0]
        phnum = struct.unpack_from("<H", e, 0x2C)[0]
        self.segs = []
        for i in range(phnum):
            t, off, va, _, fsz, _ = struct.unpack_from("<6I", e, phoff + 32 * i)
            if t == 1:
                self.segs.append((va, off, fsz))

    def file_offset(self, va):
        for v, o, s in self.segs:
            if v <= va < v + s:
                return o + va - v
        return None

    def _read_toc(self):
        e = self.elf
        words = struct.unpack_from(f"<{len(e) // 4}I", e)
        entries, archive, i = [], -1, 0
        while i < len(words) - 11:
            dat, size, sector, name, crc, y, mo, d, h, mi, idx = words[i:i + 11]
            off = self.file_offset(name) if 1990 < y < 2010 else None
            if off and 1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60 \
                    and dat < 16:
                text = e[off:e.index(b"\0", off)].decode("latin1")
                if text == "" and sector == 0:
                    archive += 1
                entries.append(dict(pos=i * 4, sector=sector, size=size,
                                    crc=crc, name=text, archive=archive))
                i += 11
            else:
                i += 1
        if archive != 3:
            raise SN2Error("Could not read the game's file table.")
        return {x["name"]: x for x in entries if x["name"]}

    def entry(self, name):
        if name not in self.toc:
            raise SN2Error(f"{name} is not in the game's file table.")
        return self.toc[name]

    def dat_path(self, entry):
        return self.folder / "DATA" / ARCHIVES[entry["archive"]]

    def dat_lba(self, entry):
        """ISO sector where the entry's .DAT starts (ISO mode only)."""
        return self.files["DATA/" + ARCHIVES[entry["archive"]]][0]

    def read(self, name):
        x = self.entry(name)
        if self.iso:
            with open(self.iso, "rb") as f:
                f.seek((self.dat_lba(x) + x["sector"]) * SECTOR)
                return f.read(x["size"])
        with open(self.dat_path(x), "rb") as f:
            f.seek(x["sector"] * SECTOR)
            return f.read(x["size"])

    def set_entry(self, name, sector, data):
        x = self.entry(name)
        size = len(data)
        crc = int(np.frombuffer(data, np.uint8).sum(dtype=np.uint64)) & 0xFFFFFFFF
        struct.pack_into("<I", self.elf, x["pos"] + 4, size)
        struct.pack_into("<I", self.elf, x["pos"] + 8, sector)
        struct.pack_into("<I", self.elf, x["pos"] + 16, crc)
        x.update(size=size, sector=sector, crc=crc)

    def record(self, code):
        if code not in self.records:
            raise SN2Error(f"No song with code {code!r}. Codes: "
                           + ", ".join(sorted(self.records)))
        return self.records[code]

    def all_records(self):
        """Yield the file offset of every record in the song table."""
        pos = self.records["andt"]
        while re.fullmatch(rb"[a-z0-9]{4}", self.elf[pos:pos + 4]):
            yield pos
            pos += RECORD

    def unlock_all(self):
        """Clear the unlock condition byte on every song; returns titles."""
        done = []
        for pos in self.all_records():
            if self.elf[pos + 7]:
                self.elf[pos + 7] = 0
                done.append(self.elf[pos:pos + 5].rstrip(b"\0").decode())
        return done

    TITLE_FALLBACK = {"mber": "Number 1 (Alan Braxe & Fred Falke Main Remix)",
                      "lab3": "La Bamba", "tuto": "Tutorial"}

    def titles(self):
        """Song code -> title, from the program's own title list."""
        e = bytes(self.elf)
        out = {}
        for code in self.records:
            for m in re.finditer(re.escape(code.encode()) + b"\x00", e):
                j = e.find(b"\0", m.end())
                text = e[m.end():j]
                if 0x220000 < m.start() < 0x22C000 and len(text) > 2 and \
                        not re.fullmatch(rb"[a-z0-9]{4,5}", text):
                    out[code] = text.decode("latin1")
                    break
            out.setdefault(code, self.TITLE_FALLBACK.get(code, code))
        return out

    def songs(self):
        """[(number, code, title)] for the 72 playable songs, in game order."""
        titles = self.titles()
        rows = sorted((self.song_index(c) + 1, c, titles[c])
                      for c in self.records if c != "tuto")
        return rows

    def song_index(self, code):
        return struct.unpack_from("<H", self.elf, self.record(code) + 8)[0] - 1


# ------------------------------------------------------------- packed lists
def split_pack(data, count=72):
    offs = list(struct.unpack_from(f"<{count}I", data))
    ends = offs[1:] + [len(data.rstrip(b"\0"))]
    return [data[a:b] for a, b in zip(offs, ends)]


def join_pack(items, align=1):
    head = 4 * len(items)
    out = bytearray(head)
    for k, item in enumerate(items):
        out += b"\0" * (-len(out) % align)
        struct.pack_into("<I", out, 4 * k, len(out))
        out += item
    return bytes(out)


def recompress(item, images):
    """Recompress one packed entry with lz_compress if that makes it smaller."""
    if images:
        h1, h2 = struct.unpack_from("<II", item)
        parts = [item[h1:h2], item[h2:]]
        new = [lz_compress(lz_decompress(p)) for p in parts]
        parts = [n if len(n) < len(p) else p for n, p in zip(new, parts)]
        return struct.pack("<II", 8, 8 + len(parts[0])) + parts[0] + parts[1]
    new = lz_compress(lz_decompress(item))
    return new if len(new) < len(item) else item


def fit_pack(items, keep, alloc, align, images, progress):
    """Join entries; if too big, recompress the others until it fits."""
    data = join_pack(items, align)
    if len(data) <= alloc:
        return data
    keep = keep if isinstance(keep, (set, frozenset)) else {keep}
    order = sorted((k for k in range(len(items)) if k not in keep),
                   key=lambda k: -len(items[k]))
    for k in order:
        items[k] = recompress(items[k], images)
        data = join_pack(items, align)
        if len(data) <= alloc:
            return data
    return data


def pad(data):
    return data + b"\0" * (-len(data) % SECTOR)


# ------------------------------------------------------------- charts
# The game unpacks one song's charts into a 0x5D00-byte buffer (found at
# every caller of its chart loader). Konami's largest is 21,112 bytes.
CHART_LIMIT = 0x5D00
LEVEL_INDEX = {(0x14, 4): 0, (0x14, 1): 1, (0x14, 2): 2, (0x14, 3): 3, (0x14, 6): 4,
               (0x18, 4): 5, (0x18, 1): 6, (0x18, 2): 7, (0x18, 3): 8, (0x18, 6): 9}
DIFF_NAME = {1: "Basic", 2: "Difficult", 3: "Expert", 4: "Beginner", 6: "Challenge"}


def ps2_chart(song, notes=None):
    """Convert a parsed StepMania song to an SN2 chart (SSQ bytes).

    Returns (chart, levels, shift). levels follows sm2ddr's 10-slot layout,
    giving each SN2 difficulty the level of the chart placed in it.
    notes, if a list, receives messages about simplified charts.
    """
    notes = notes if notes is not None else []
    args = argparse.Namespace(tps=TPS)
    data, levels, shift = sm2ddr.convert_charts(song, args)
    chunks = sm2ddr.read_ssq(data)
    pos, raw = 0, []
    while pos + 4 <= len(data):
        size = struct.unpack_from("<I", data, pos)[0]
        if size == 0:
            break
        raw.append(data[pos:pos + size])
        pos += size
    tempo, triggers = raw[0], raw[1]
    steps = {(c["p2"] & 0xFF, c["p2"] >> 8): r
             for c, r in zip(chunks[2:], raw[2:])}
    steps = {k: v for k, v in steps.items() if k in LEVEL_INDEX}
    if not steps:
        raise SN2Error("The song has no single or double charts.")
    real_level = {k: levels[LEVEL_INDEX[k]] for k in steps}
    # SN2 order (as Konami's charts): single basic, difficult, expert, beginner,
    # challenge; double basic, difficult, expert, challenge. Challenge charts
    # (SSQ difficulty 6; Konami's own on NGO, TRIP MACHINE PhoeniX, PARANOiA
    # ~HADES~, Fascination ~eternal love mix~) go in only when the song has
    # one. The other difficulties must all exist: a missing one borrows the
    # nearest chart the song has.
    order = [(0x14, 1), (0x14, 2), (0x14, 3), (0x14, 4)] + \
        ([(0x14, 6)] if (0x14, 6) in steps else []) + \
        [(0x18, 1), (0x18, 2), (0x18, 3)] + \
        ([(0x18, 6)] if (0x18, 6) in steps else [])
    near = {1: [1, 4, 2, 3, 6], 2: [2, 3, 1, 6, 4], 3: [3, 2, 6, 1, 4],
            4: [4, 1, 2, 3, 6], 6: [6]}
    placed = {}
    for style, diff in order:
        pool = [(style, d) for d in near[diff]]
        if diff != 6:
            pool += [(0x14 if style == 0x18 else 0x18, d) for d in near[diff]]
        placed[(style, diff)] = next(k for k in pool if k in steps)

    def total():
        return len(tempo) + len(triggers) + 4 + \
            sum(len(steps[placed[k]]) for k in order)
    smallest = min(steps, key=lambda k: len(steps[k]))
    if total() > CHART_LIMIT:
        # 0. Challenge charts are the first to go
        for k in [(0x18, 6), (0x14, 6)]:
            if k in order and total() > CHART_LIMIT:
                order.remove(k)
                notes.append(f"{'SP' if k[0] == 0x14 else 'DP'} Challenge left out "
                             f"(too many notes for the game)")
        smallest = min((placed[k] for k in order), key=lambda k: len(steps[k]))
    if total() > CHART_LIMIT:
        # 1. charts that were only copies stand in with the smallest chart
        for k in order:
            if placed[k] != k and total() > CHART_LIMIT:
                placed[k] = smallest
        # 2. then the heaviest doubles, then singles Expert and Difficult
        for k in [(0x18, 3), (0x18, 2), (0x18, 1), (0x14, 3), (0x14, 2)]:
            if total() <= CHART_LIMIT:
                break
            if placed[k] != smallest:
                was = placed[k]
                placed[k] = smallest
                style = "SP" if k[0] == 0x14 else "DP"
                notes.append(f"{style} {DIFF_NAME[k[1]]} uses the "
                             f"{'SP' if smallest[0] == 0x14 else 'DP'} "
                             f"{DIFF_NAME[smallest[1]]} chart (too many notes "
                             f"for the game)")
                del was
        if total() > CHART_LIMIT:
            raise SN2Error("has too many notes for SuperNOVA 2 even with only "
                           "its easiest chart")
        if not notes:
            notes.append("copied charts were simplified to fit the game")
    out = tempo + triggers
    new_levels = [0] * 10
    order = sorted(order, key=lambda k: (k[0], {1: 0, 2: 1, 3: 2, 4: 3, 6: 4}[k[1]]))
    for style, diff in order:
        src = placed[(style, diff)]
        block = bytearray(steps[src])
        struct.pack_into("<H", block, 6, diff << 8 | style)
        out += bytes(block)
        new_levels[LEVEL_INDEX[(style, diff)]] = real_level[src]
    out += b"\0\0\0\0"
    assert len(out) <= CHART_LIMIT
    return out, new_levels, shift


# Modern 1-19 level -> SuperNOVA 2 feet, taken from the most common pairing
# across the songs SuperNOVA 2 shares with the A20 PLUS omnimix.
OLD_FEET = {1: 1, 2: 2, 3: 3, 4: 3, 5: 4, 6: 5, 7: 5, 8: 6, 9: 7, 10: 7,
            11: 8, 12: 8, 13: 9, 14: 9, 15: 9, 16: 9, 17: 10, 18: 10, 19: 10}


def old_scale(meter):
    """Map a modern 1-19 meter to SuperNOVA 2's 1-10 feet."""
    if not meter:
        return 0
    return OLD_FEET[max(1, min(19, meter))]


# ------------------------------------------------------------- audio (VIG)
COEF = np.array([(0, 0), (60, 0), (115, -52), (98, -55), (122, -60)], np.int64)


def adpcm_encode(x):
    """Encode int64 samples (lanes x samples, samples % 28 == 0) as PS-ADPCM.

    Lanes are encoded in parallel; each lane is one channel segment.
    Returns uint8 array (lanes, frames, 16).
    """
    lanes, total = x.shape
    frames = total // 28
    out = np.zeros((lanes, frames, 16), np.uint8)
    s1 = np.zeros(lanes, np.int64)
    s2 = np.zeros(lanes, np.int64)
    for f in range(frames):
        blk = x[:, f * 28:(f + 1) * 28]
        best_err = np.full(lanes, np.inf)
        best = None
        # try every filter with its own step size; keep the least error
        for pred, (c1, c2) in enumerate(COEF):
            hist = np.concatenate([np.stack([s2, s1], 1), blk], 1)
            res = hist[:, 2:] - ((hist[:, 1:-1] * c1 + hist[:, :-2] * c2) >> 6)
            peak = np.abs(res).max(1)
            shift = np.zeros(lanes, np.int64)
            for s in range(13):
                shift = np.where(peak <= (7 << 12) >> s, s, shift)
            a1, a2 = s1.copy(), s2.copy()
            nibs = np.zeros((lanes, 28), np.int64)
            err = np.zeros(lanes)
            for k in range(28):
                guess = (a1 * c1 + a2 * c2) >> 6
                q = np.clip(np.rint(((blk[:, k] - guess) << shift) / 4096.0)
                            .astype(np.int64), -8, 7)
                val = np.clip(((q << 12) >> shift) + guess, -32768, 32767)
                err += (blk[:, k] - val) ** 2.0
                nibs[:, k] = q & 15
                a2, a1 = a1, val
            better = err < best_err
            best_err = np.where(better, err, best_err)
            if best is None:
                best = [np.full(lanes, pred), shift, nibs, a1, a2]
            else:
                best[0] = np.where(better, pred, best[0])
                best[1] = np.where(better, shift, best[1])
                best[2] = np.where(better[:, None], nibs, best[2])
                best[3] = np.where(better, a1, best[3])
                best[4] = np.where(better, a2, best[4])
        pred, shift, nibs, s1, s2 = best
        out[:, f, 0] = (pred << 4 | shift).astype(np.uint8)
        out[:, f, 2:] = (nibs[:, 0::2] | nibs[:, 1::2] << 4).astype(np.uint8)
    return out


def vig_encode(pcm, header, segment_frames=512):
    """Stereo int64 PCM -> VIG bytes, reusing a stock 2 KB header."""
    total = math.ceil(len(pcm) / 28) * 28
    x = np.zeros((total, 2), np.int64)
    x[:len(pcm)] = pcm
    frames = total // 28
    seg = segment_frames
    nseg = math.ceil(frames / seg)
    padded = np.zeros((nseg * seg * 28, 2), np.int64)
    padded[:total] = x
    lanes = padded.reshape(nseg, seg * 28, 2).transpose(2, 0, 1) \
        .reshape(2 * nseg, seg * 28)
    enc = adpcm_encode(lanes).reshape(2, nseg * seg, 16)[:, :frames]
    body = np.stack([enc[0], enc[1]], 1).reshape(-1)  # L, R frames interleaved
    head = bytearray(header[:SECTOR])
    struct.pack_into("<I", head, 12, len(body))
    return bytes(head) + body.tobytes()


def vig_decode(data):
    start, size = struct.unpack_from("<II", data, 8)
    body = np.frombuffer(data[start:start + size], np.uint8).reshape(-1, 2, 16)
    out = []
    for ch in range(2):
        fr = body[:, ch]
        s1 = s2 = 0
        samples = np.zeros(len(fr) * 28, np.int64)
        for f in range(len(fr)):
            pred, shift = int(fr[f, 0]) >> 4, int(fr[f, 0]) & 15
            c1, c2 = (int(v) for v in COEF[pred]) if pred < 5 else (0, 0)
            for k in range(28):
                nib = (int(fr[f, 2 + k // 2]) >> (4 * (k & 1))) & 15
                nib -= 16 if nib >= 8 else 0
                v = ((nib << 12) >> shift) + ((s1 * c1 + s2 * c2) >> 6)
                v = max(-32768, min(32767, v))
                samples[f * 28 + k] = v
                s2, s1 = s1, v
        out.append(samples)
    return np.stack(out, 1)


# ------------------------------------------------------------- images
FFMPEG_MISSING = ("ffmpeg was not found. Put ffmpeg.exe in the same folder as this tool "
                  "(or install ffmpeg so it is on PATH), then try again.")


def need_ffmpeg():
    """The ffmpeg program, or a clear error if it isn't there."""
    find_ffmpeg()
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SN2Error(FFMPEG_MISSING)
    return exe


def ffmpeg_pal8(src, width, height, colors, extra="", dither="none"):
    """Scale an image and quantize it; returns (indices, rgba palette)."""
    exe = need_ffmpeg()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "pal.bmp"
        graph = (f"[0:v]scale={width}:{height}:flags=lanczos{extra},split[a][b];"
                 f"[a]palettegen=max_colors={colors}:reserve_transparent=0[p];"
                 f"[b][p]paletteuse=dither={dither}")
        run = subprocess.run([exe, "-v", "error", "-y", "-i", str(src),
                              "-filter_complex", graph, "-frames:v", "1",
                              "-pix_fmt", "pal8", str(out)], capture_output=True)
        if run.returncode:
            raise SN2Error(f"ffmpeg failed: {run.stderr[:300]!r}")
        bmp = out.read_bytes()
    off, = struct.unpack_from("<I", bmp, 10)
    hsize, w, h = struct.unpack_from("<Iii", bmp, 14)
    ncol = struct.unpack_from("<I", bmp, 46)[0] or 256
    pal = np.frombuffer(bmp, np.uint8, ncol * 4, 14 + hsize).reshape(-1, 4)
    rgba = np.zeros((256, 4), np.uint8)
    rgba[:ncol, 0], rgba[:ncol, 1], rgba[:ncol, 2] = pal[:, 2], pal[:, 1], pal[:, 0]
    rgba[:, 3] = 255
    stride = (w + 3) & ~3
    rows = np.frombuffer(bmp, np.uint8, stride * abs(h), off).reshape(abs(h), stride)
    idx = rows[:, :w]
    if h > 0:
        idx = idx[::-1]
    return idx.copy(), rgba


def clut_swizzle(pal):
    c = pal.copy()
    for i in range(0, 256, 32):
        c[i + 8:i + 16], c[i + 16:i + 24] = pal[i + 16:i + 24], pal[i + 8:i + 16]
    return c


def tim2_replace(template, pixels, clut):
    """Copy a one-picture TIM2, swapping in new pixel and CLUT bytes."""
    q = 16
    tot, clut_size, img_size, hs = struct.unpack_from("<IIIH", template, q)
    if len(pixels) != img_size or len(clut) != clut_size:
        raise SN2Error("image does not match the template size")
    return template[:q + hs] + pixels + clut + template[q + tot:]


def banner_tim2(template, image, dither="none", colors=256):
    idx, pal = ffmpeg_pal8(image, 256, 80, colors, dither=dither)
    pal = pal.copy()
    pal[:, 3] = 0x80
    return tim2_replace(template, idx.astype(np.uint8).tobytes(),
                        clut_swizzle(pal).tobytes())


def _draw_line(text, size, height):
    exe = need_ffmpeg()
    font = next((f for f in (Path("C:/Windows/Fonts/arialbd.ttf"),
                             Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
                 if f.exists()), None)
    fontarg = ""
    if font:
        fontarg = ":fontfile='" + str(font).replace("\\", "/") \
            .replace(":", "\\:") + "'"
    text = text.replace("\\", "\\\\").replace("'", "\u2019") \
        .replace(":", "\\:").replace("%", "\\%")
    graph = (f"color=c=black:s=600x{height},format=gray,"
             f"drawtext=text='{text}'{fontarg}:fontsize={size}:"
             f"fontcolor=white:x=0:y=(h-th)/2")
    run = subprocess.run([exe, "-v", "error", "-f", "lavfi", "-i", graph,
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray",
                          "-"], capture_output=True)
    if run.returncode or len(run.stdout) != 600 * height:
        raise SN2Error(f"could not draw the title: {run.stderr[:300]!r}")
    img = np.frombuffer(run.stdout, np.uint8).reshape(height, 600).astype(int)
    lo = int(img.min())  # ffmpeg's gray black can be 16, not 0
    return np.clip((img - lo) * 255 // max(1, 255 - lo), 0, 255).astype(np.uint8)


def _fit_line(text, size, height, width=220):
    """Draw text, shrinking the font (then squeezing) until it fits width."""
    for s in range(size, 8, -1):
        img = _draw_line(text, s, height)
        cols = np.nonzero(img.max(0))[0]
        right = cols[-1] + 1 if len(cols) else 0
        if right <= width:
            return img[:, :width]
    cols = np.nonzero(img.max(0))[0]
    right = cols[-1] + 1
    xs = (np.arange(width) * right / width).astype(int)
    return img[:, xs]


def title_tim2(template, title, artist):
    """Draw title and artist in white on the 224x32 4-bit label."""
    canvas = np.zeros((32, 224), np.uint8)
    canvas[0:18, 2:222] = _fit_line(title, 15, 18)
    canvas[18:32, 2:222] = _fit_line(artist or "", 11, 14)
    level = (canvas.astype(int) * 15 + 127) // 255
    packed = (level[:, 0::2] | level[:, 1::2] << 4).astype(np.uint8).tobytes()
    q = 16
    tot, clut_size, img_size, hs = struct.unpack_from("<IIIH", template, q)
    clut = template[q + hs + img_size:q + hs + img_size + clut_size]
    return tim2_replace(template, packed, clut)


# ------------------------------------------------------------- ISO 9660
def both16(v):
    return struct.pack("<H", v) + struct.pack(">H", v)


def both32(v):
    return struct.pack("<I", v) + struct.pack(">I", v)


def dir_record(name, lba, size, is_dir, when):
    ident = name if isinstance(name, bytes) else name.encode("latin1")
    length = 33 + len(ident) + (len(ident) + 1) % 2
    rec = bytearray(length)
    rec[0] = length
    rec[2:10] = both32(lba)
    rec[10:18] = both32(size)
    rec[18:25] = when
    rec[25] = 2 if is_dir else 0
    rec[28:32] = both16(1)
    rec[32] = len(ident)
    rec[33:33 + len(ident)] = ident
    return bytes(rec)


def _crc16(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _udf_tag(ident, location, body):
    """Prefix a UDF descriptor body (bytes after the tag) with its tag."""
    tag = bytearray(struct.pack("<HHBBHHHI", ident, 2, 0, 0, 0, _crc16(body),
                                len(body), location))
    tag[4] = sum(tag[0:4] + tag[5:16]) & 0xFF
    return bytes(tag) + body


def _regid(text, suffix=b""):
    return b"\0" + text.encode().ljust(23, b"\0") + suffix.ljust(8, b"\0")


def _charspec():
    return b"\0" + b"OSTA Compressed Unicode".ljust(63, b"\0")


def _dstring(text, size):
    raw = b"\x08" + text.encode("latin1")[:size - 2]
    return raw.ljust(size - 1, b"\0") + bytes((len(raw),))


def _udf_time(t):
    return struct.pack("<HHBBBBBBBB", 0x1000, t.tm_year, t.tm_mon, t.tm_mday,
                       t.tm_hour, t.tm_min, t.tm_sec, 0, 0, 0)


def _udf_domain():
    return _regid("*OSTA UDF Compliant", struct.pack("<HB", 0x0102, 0))


def _udf_impl():
    return _regid("*SN2 Song Swap")


def write_iso(folder, out_path, overrides, volume="SLUS_216.08", progress=print):
    """Write folder as a PS2 DVD image: ISO 9660 plus a UDF 1.02 bridge.

    overrides maps a path relative to folder (with / separators) to
    (size, writer); writer(file) writes the replacement content.
    """
    folder = Path(folder)
    t = time.gmtime()
    when = bytes((t.tm_year - 1900, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min,
                  t.tm_sec, 0))

    def scan(path, rel):
        node = dict(name=path.name, rel=rel, dirs=[], files=[])
        for child in sorted(path.iterdir(), key=lambda p: p.name.upper()):
            crel = f"{rel}/{child.name}" if rel else child.name
            if child.is_dir():
                node["dirs"].append(scan(child, crel))
            else:
                size = overrides[crel][0] if crel in overrides else child.stat().st_size
                node["files"].append(dict(name=child.name, rel=crel, size=size,
                                          path=child))
        return node

    root = scan(folder, "")
    root["parent"] = None
    dirs, queue = [], [root]
    while queue:  # breadth first, as the path tables need
        node = queue.pop(0)
        dirs.append(node)
        for d in node["dirs"]:
            d["parent"] = node
            queue.append(d)
    for i, d in enumerate(dirs):
        d["num"] = i + 1
    files = [f for d in dirs for f in d["files"]]

    def ident(name):
        return name.upper().encode("latin1")

    # ---- layout
    partition = 257                      # UDF partition start (after AVDP)
    lba = partition + 2                  # FSD + terminator
    unique = 16
    for d in dirs:                       # UDF file entry + identifiers
        fids = 40  # parent entry
        for c in d["dirs"]:
            fids += 4 * math.ceil((38 + 1 + len(c["name"])) / 4)
        for f in d["files"]:
            fids += 4 * math.ceil((38 + 1 + len(f["name"])) / 4)
        d["fid_size"] = fids
        d["fe"] = lba
        d["fid_lba"] = lba + 1
        lba += 1 + math.ceil(fids / SECTOR)
        d["uid"] = 0 if d is root else unique
        unique += 0 if d is root else 1
    for f in files:
        f["fe"] = lba
        f["uid"] = unique
        unique += 1
        lba += 1
    pt_size = sum(8 + (1 if d is root else len(ident(d["name"])))
                  + (1 if d is root else len(ident(d["name"]))) % 2 for d in dirs)
    pt_sectors = math.ceil(pt_size / SECTOR)
    l_table, m_table = lba, lba + pt_sectors
    lba += 2 * pt_sectors

    def dir_size(d):
        used, size = 0, 0
        names = [ident(c["name"]) for c in d["dirs"]] + \
                [ident(f["name"]) + b";1" for f in d["files"]]
        for nm in [b"\0", b"\1"] + names:
            rec = 33 + len(nm) + (len(nm) + 1) % 2
            if used + rec > SECTOR:
                size += SECTOR
                used = 0
            used += rec
        return size + SECTOR
    for d in dirs:
        d["size"] = dir_size(d)
        d["lba"] = lba
        lba += d["size"] // SECTOR
    for f in files:
        f["lba"] = lba
        lba += max(1, math.ceil(f["size"] / SECTOR))
    end_avdp = lba
    total = lba + 1
    part_len = end_avdp - partition

    # ---- ISO 9660
    def dir_bytes(d):
        out = bytearray()
        parent = d["parent"] or d

        def add(rec):
            nonlocal out
            if len(out) // SECTOR != (len(out) + len(rec) - 1) // SECTOR:
                out += b"\0" * (-len(out) % SECTOR)
            out += rec
        add(dir_record(b"\0", d["lba"], d["size"], True, when))
        add(dir_record(b"\1", parent["lba"], parent["size"], True, when))
        entries = [(ident(c["name"]), c, True) for c in d["dirs"]] + \
                  [(ident(f["name"]) + b";1", f, False) for f in d["files"]]
        for nm, item, is_dir in sorted(entries, key=lambda e: e[0]):
            add(dir_record(nm, item["lba"], item["size"], is_dir, when))
        if len(out) > d["size"]:
            raise SN2Error("directory too large")
        return bytes(out) + b"\0" * (d["size"] - len(out))

    def path_table(big):
        fmt = ">" if big else "<"
        out = bytearray()
        for d in dirs:
            nm = b"\0" if d is root else ident(d["name"])
            parent = (d["parent"] or d)["num"]
            out += bytes((len(nm), 0)) + struct.pack(fmt + "I", d["lba"]) + \
                struct.pack(fmt + "H", parent) + nm + b"\0" * (len(nm) % 2)
        return bytes(out)

    def iso_pvd():
        v = bytearray(SECTOR)
        v[0], v[1:6], v[6] = 1, b"CD001", 1
        v[8:40] = b"PLAYSTATION".ljust(32)
        v[40:72] = volume.encode().ljust(32)
        v[80:88] = both32(total)
        v[120:124] = both16(1)
        v[124:128] = both16(1)
        v[128:132] = both16(SECTOR)
        v[132:140] = both32(pt_size)
        v[140:144] = struct.pack("<I", l_table)
        v[148:152] = struct.pack(">I", m_table)
        v[156:190] = dir_record(b"\0", root["lba"], root["size"], True, when)
        v[190:813] = b" " * (813 - 190)
        stamp = time.strftime("%Y%m%d%H%M%S00", t).encode() + b"\0"
        v[813:830] = stamp
        v[830:847] = stamp
        v[847:864] = b"0" * 16 + b"\0"
        v[864:881] = b"0" * 16 + b"\0"
        v[881] = 1
        return bytes(v)

    # ---- UDF 1.02
    ts = _udf_time(t)

    def extent(length, loc):
        return struct.pack("<II", length, loc)

    def long_ad(length, lbn):
        return struct.pack("<IIH", length, lbn, 0) + b"\0" * 6

    def sector(data):
        return data + b"\0" * (SECTOR - len(data))

    def vds(base):
        pvd = struct.pack("<II", 0, 0) + _dstring(volume, 32) + \
            struct.pack("<HHHHII", 1, 1, 2, 2, 1, 1) + \
            _dstring(volume + "_SET", 128) + _charspec() + _charspec() + \
            extent(0, 0) + extent(0, 0) + _regid("") + ts + _udf_impl() + \
            b"\0" * 64 + struct.pack("<IH", 0, 0) + b"\0" * 22
        iuvd = struct.pack("<I", 1) + \
            _regid("*UDF LV Info", struct.pack("<HBB", 0x0102, 0, 0)) + \
            _charspec() + _dstring(volume, 128) + b"\0" * 108 + \
            _udf_impl() + b"\0" * 128
        pd = struct.pack("<IHH", 2, 1, 0) + _regid("+NSR02") + b"\0" * 128 + \
            struct.pack("<III", 1, partition, part_len) + _udf_impl() + \
            b"\0" * 128 + b"\0" * 156
        lvd = struct.pack("<I", 3) + _charspec() + _dstring(volume, 128) + \
            struct.pack("<I", SECTOR) + _udf_domain() + long_ad(SECTOR, 0) + \
            struct.pack("<II", 6, 1) + _udf_impl() + b"\0" * 128 + \
            extent(2 * SECTOR, 64) + struct.pack("<BBHH", 1, 6, 1, 0)
        usd = struct.pack("<II", 4, 0)
        td = b"\0" * 496
        parts = [(1, pvd), (4, iuvd), (5, pd), (6, lvd), (7, usd), (8, td)]
        return [sector(_udf_tag(i, base + k, body)) for k, (i, body) in enumerate(parts)]

    def lvid():
        impl = _udf_impl() + struct.pack("<IIHHH", len(files), len(dirs),
                                         0x0102, 0x0102, 0x0102)
        body = ts + struct.pack("<I", 1) + extent(0, 0) + \
            struct.pack("<Q", unique) + b"\0" * 24 + \
            struct.pack("<IIII", 1, len(impl), 0, part_len) + impl
        return sector(_udf_tag(9, 64, body)) + \
            sector(_udf_tag(8, 65, b"\0" * 496))

    def avdp(where):
        body = extent(16 * SECTOR, 32) + extent(16 * SECTOR, 48) + b"\0" * 480
        return sector(_udf_tag(2, where, body))

    def fsd():
        body = ts + struct.pack("<HHIIII", 3, 3, 1, 1, 0, 0) + _charspec() + \
            _dstring(volume, 128) + _charspec() + _dstring(volume, 32) + \
            _dstring("", 32) + _dstring("", 32) + \
            long_ad(SECTOR, root["fe"] - partition) + _udf_domain() + \
            long_ad(0, 0) + b"\0" * 48
        return sector(_udf_tag(256, 0, body)) + sector(_udf_tag(8, 1, b"\0" * 496))

    def file_entry(lbn, is_dir, size, data_lba, uid, links):
        ads = b""
        left, loc = size, data_lba - partition
        while left > 0:
            part = min(left, (1 << 30) - SECTOR)
            ads += struct.pack("<II", part, loc)
            left -= part
            loc += part // SECTOR
        icb = struct.pack("<IHHHBB", 0, 4, 0, 1, 0, 4 if is_dir else 5) + \
            b"\0" * 6 + struct.pack("<H", 0)
        body = icb + struct.pack("<IIIHBBI", 0xFFFFFFFF, 0xFFFFFFFF, 0x14A5,
                                 links, 0, 0, 0) + \
            struct.pack("<QQ", size, math.ceil(size / SECTOR)) + ts + ts + ts + \
            struct.pack("<I", 1) + long_ad(0, 0) + _udf_impl() + \
            struct.pack("<QII", uid, 0, len(ads)) + ads
        return sector(_udf_tag(261, lbn, body))

    def fids(d):
        out = bytearray()
        block = d["fid_lba"] - partition

        def fid(chars, name, target):
            nm = (b"\x08" + name.encode("latin1")) if name else b""
            body = struct.pack("<HBB", 1, chars, len(nm)) + \
                long_ad(SECTOR, target - partition) + struct.pack("<H", 0) + nm
            body += b"\0" * (-(16 + len(body)) % 4)
            return _udf_tag(257, block + len(out) // SECTOR, body)
        parent = d["parent"] or d
        out += fid(0x0A, "", parent["fe"])
        for c in d["dirs"]:
            out += fid(0x02, c["name"], c["fe"])
        for f in d["files"]:
            out += fid(0x00, f["name"], f["fe"])
        return bytes(out)

    with open(out_path, "wb") as f:
        f.write(b"\0" * 16 * SECTOR)
        f.write(iso_pvd())
        term = bytearray(SECTOR)
        term[0], term[1:6], term[6] = 255, b"CD001", 1
        f.write(term)
        for name in (b"BEA01", b"NSR02", b"TEA01"):
            f.write(sector(b"\0" + name + b"\1"))
        f.write(b"\0" * (32 - 21) * SECTOR)
        for base in (32, 48):
            for s in vds(base):
                f.write(s)
            f.write(b"\0" * 10 * SECTOR)
        f.write(lvid())
        f.write(b"\0" * (256 - 66) * SECTOR)
        f.write(avdp(256))
        f.write(fsd())
        for d in dirs:
            assert f.tell() == d["fe"] * SECTOR
            links = 1 + len(d["dirs"])
            data = fids(d)
            assert len(data) == d["fid_size"]
            f.write(file_entry(d["fe"] - partition, True, len(data), d["fid_lba"],
                               d["uid"], links))
            f.write(pad(data))
        for item in files:
            f.write(file_entry(item["fe"] - partition, False, item["size"],
                               item["lba"], item["uid"], 1))
        assert f.tell() == l_table * SECTOR
        for big in (False, True):
            f.write(pad(path_table(big)))
        for d in dirs:
            f.write(dir_bytes(d))
        for item in files:
            assert f.tell() == item["lba"] * SECTOR, item["rel"]
            if item["rel"] in overrides:
                overrides[item["rel"]][1](f)
            else:
                with open(item["path"], "rb") as src:
                    shutil.copyfileobj(src, f, 8 << 20)
            f.write(b"\0" * (-f.tell() % SECTOR))
            want = (item["lba"] + max(1, math.ceil(item["size"] / SECTOR))) * SECTOR
            if f.tell() < want:
                f.write(b"\0" * (want - f.tell()))
            progress(f"wrote {item['rel']}")
        assert f.tell() == end_avdp * SECTOR
        f.write(avdp(end_avdp))
    return total * SECTOR


# ------------------------------------------------------------- replace
def open_song(song_path, keep):
    """Return the simfile for a song folder, zip or .sm/.ssc path.

    Zips are extracted into a temporary folder that is appended to keep.
    """
    song_path = Path(str(song_path).strip().strip('"'))
    if not song_path.exists():
        raise SN2Error(f"{song_path} was not found.")
    if song_path.suffix.lower() == ".zip":
        import zipfile
        tmp = tempfile.TemporaryDirectory()
        keep.append(tmp)
        with zipfile.ZipFile(song_path) as z:
            z.extractall(tmp.name)
        song_path = Path(tmp.name)
    if song_path.is_file():
        simfile = song_path
    else:
        found = sorted(p for p in song_path.rglob("*")
                       if p.suffix.lower() in (".sm", ".ssc"))
        simfile = next((p for p in found if p.suffix.lower() == ".sm"), None) \
            or (found[0] if found else None)
    if simfile is None:
        raise SN2Error(f"No .sm or .ssc file in {song_path.name}.")
    if simfile.suffix.lower() not in (".sm", ".ssc"):
        raise SN2Error("Pick a song folder, a zip, or an .sm/.ssc file.")
    return simfile


def find_simfiles(folder):
    """Every song in a folder: one simfile per song folder (.sm preferred)."""
    by_dir = {}
    for p in sorted(Path(folder).rglob("*")):
        if p.suffix.lower() in (".sm", ".ssc"):
            cur = by_dir.get(p.parent)
            if cur is None or (cur.suffix.lower() == ".ssc"
                               and p.suffix.lower() == ".sm"):
                by_dir[p.parent] = p
    return [by_dir[k] for k in sorted(by_dir)]


def song_art(song, simfile):
    images = dict(song.images)
    for tag in ("BANNER", "BACKGROUND", "JACKET"):
        name = images.get(tag)
        if name and (simfile.parent / name).exists():
            return simfile.parent / name
    return sm2ddr.find_art(song)


# Byte 62 of a song record holds flags. Bit 0x20 is set on every SuperNOVA 2
# song that has a Challenge chart (NGO, TRIP MACHINE PhoeniX, PARANOiA ~HADES~,
# Fascination ~eternal love mix~ and one more), and the game only offers
# Challenge on songs with it.
FLAG_BYTE, CHALLENGE_FLAG = 62, 0x20


def set_record(game, slot, song, levels):
    rec = game.record(slot)
    if levels[4] or levels[9]:
        game.elf[rec + FLAG_BYTE] |= CHALLENGE_FLAG
    bpms = [b for _, b in song.bpms]
    lo, hi = round(min(bpms)), round(max(bpms))
    struct.pack_into("<HH", game.elf, rec + 32, min(hi, 999), min(lo, 999))

    # levels: basic, difficult, expert in the low three nibbles. Missing
    # charts borrowed another chart, so give them that chart's level.
    def feet(slots, other):
        got = [old_scale(levels[i]) for i in slots]
        if not any(got):
            got = [old_scale(levels[i]) for i in other]
        known = [v for v in got if v] or [1]
        return [v or known[0] for v in got]
    # the top nibble is the Challenge level (0 = the song has no Challenge;
    # Konami's challenge songs carry it there, e.g. NGO 0xA976)
    for off, slots, other, ch in ((52, (1, 2, 3), (6, 7, 8), 4),
                                  (56, (6, 7, 8), (1, 2, 3), 9)):
        lv = feet(slots, other)
        chal = min(15, old_scale(levels[ch])) if levels[ch] else 0
        struct.pack_into("<H", game.elf, rec + off,
                         lv[0] | lv[1] << 4 | lv[2] << 8 | chal << 12)


class Placer:
    """Writes data into a copied disc image: in place when it fits the old
    space, otherwise in unused space after the last file on the disc."""

    def __init__(self, game, f):
        self.game, self.f = game, f
        end = max(lba + math.ceil(size / SECTOR)
                  for lba, size in game.files.values())
        self.first = self.free = end + 16

    def put(self, name, data, progress):
        game = self.game
        x = game.entry(name)
        base = game.dat_lba(x)
        if len(data) <= x["size"]:
            data = data + b"\0" * (x["size"] - len(data))
            lba = base + x["sector"]
        else:
            count = math.ceil(len(data) / SECTOR)
            # UDF can keep an anchor 256 sectors before the end; stay clear
            if self.free < game.total and self.free + count > game.total - 260:
                self.free = game.total
            lba = self.free
            self.free += count
        self.f.seek(lba * SECTOR)
        self.f.write(data)
        game.set_entry(name, lba - base, data)

    def finish(self):
        """Grow the image if data went past its end; returns True if so."""
        self.f.seek(0, 2)
        size = self.f.tell()
        need = self.free * SECTOR
        if self.free > self.first and need > size:
            self.f.write(b"\0" * (need - size))
            # keep the ISO 9660 volume size in step with the file
            self.f.seek(16 * SECTOR + 80)
            self.f.write(both32(self.free))
            return True
        return False


def replace_many(iso, items, out_iso, progress=print, cancelled=lambda: False):
    """Replace several songs on a copy of the original disc image.

    items: list of (song path, slot code). Each slot can be used once.
    """
    game = Game(iso)
    if not game.iso:
        raise SN2Error("Choose the original SuperNOVA 2 .iso file.")
    if Path(out_iso).resolve() == Path(game.iso).resolve():
        raise SN2Error("Choose a different file to save to; the original is kept.")
    need_ffmpeg()          # titles, banners and audio all need it: say so before any work
    # items may carry a third value: True/False = turn Challenge on/off for that
    # song (default: on when the song has a Challenge chart)
    items = [tuple(x) + (None,) * (3 - len(x)) for x in items]
    slots = [slot for _, slot, _ in items]
    dupes = sorted({s for s in slots if slots.count(s) > 1})
    if dupes:
        raise SN2Error("Each song can only be replaced once: " + ", ".join(dupes))
    for slot in slots:
        game.record(slot)
    keep = []
    csq_name, dt_name = "mdb/csq_all.bin", "mdb/music_dt.bin"
    csq = split_pack(game.read(csq_name))
    dt = split_pack(game.read(dt_name))
    jobs = []
    # 1. read every song and build its chart, before any slow work
    for n, (path, slot, challenge) in enumerate(items, 1):
        simfile = open_song(path, keep)
        song = sm2ddr.parse_simfile(simfile)
        notes = []
        try:
            chart, levels, shift = ps2_chart(song, notes)
        except SN2Error as e:
            raise SN2Error(f"{song.title}: {e}")
        for note in notes:
            progress(f"  {song.title}: {note}")
        index = game.song_index(slot)
        if challenge is False:          # Challenge turned off: no Challenge level
            levels = list(levels); levels[4] = levels[9] = 0
        jobs.append(dict(simfile=simfile, song=song, slot=slot, index=index,
                         chart=chart, levels=levels, shift=shift))
        progress(f"Read {n} of {len(items)}: {song.title}")
    # 2. charts
    for j in jobs:
        csq[j["index"]] = lz_compress(j["chart"])
        if lz_decompress(csq[j["index"]]) != j["chart"]:
            raise SN2Error("internal error: chart compression check failed")
    keep_idx = {j["index"] for j in jobs}
    csq_alloc = game.entry(csq_name)["size"]
    csq_data = fit_pack(csq, keep_idx, csq_alloc, 1, False, progress)
    if len(csq_data) > csq_alloc:
        progress(f"Note: the chart file grew from {csq_alloc // 1024} KB to "
                 f"{len(csq_data) // 1024} KB and was moved to free space. If the "
                 "game will not start, try fewer songs.")
    # 3. title labels and banners; fewer colors if they do not all fit
    progress("Drawing banners")
    templates = {}
    for j in jobs:
        entry = dt[j["index"]]
        h1, h2 = struct.unpack_from("<II", entry)
        templates[j["index"]] = (lz_decompress(entry[h1:h2]),
                                 lz_decompress(entry[h2:]))
        j["title_lz"] = lz_compress(title_tim2(templates[j["index"]][0],
                                               j["song"].title, j["song"].artist))
        j["art"] = song_art(j["song"], j["simfile"])
    dt_alloc = game.entry(dt_name)["size"]
    for colors in (256, 128, 96, 64, 48):
        for j in jobs:
            banner = templates[j["index"]][1]
            if j["art"]:
                banner = banner_tim2(banner, j["art"], colors=colors)
            b_lz = lz_compress(banner)
            dt[j["index"]] = struct.pack("<II", 8, 8 + len(j["title_lz"])) + \
                j["title_lz"] + b_lz
        if len(join_pack(dt, 4)) <= dt_alloc:
            break
        progress(f"Banners do not fit with {colors} colors; trying fewer")
    dt_data = fit_pack(dt, keep_idx, dt_alloc, 4, True, progress)
    if len(dt_data) > dt_alloc:
        progress(f"Note: the banner file grew from {dt_alloc // 1024} KB to "
                 f"{len(dt_data) // 1024} KB and was moved to free space. If the "
                 "game will not start, try fewer songs.")
    # 4. copy the disc, then encode and write audio one song at a time
    progress("Copying the disc image")
    shutil.copyfile(game.iso, out_iso)
    try:
        with open(out_iso, "r+b") as f:
            place = Placer(game, f)
            for n, j in enumerate(jobs, 1):
                if cancelled():
                    raise SN2Error("Stopped.")
                song, slot = j["song"], j["slot"]
                progress(f"Audio {n} of {len(jobs)}: {song.title}")
                music_file = sm2ddr.find_file(
                    j["simfile"].parent, song.music,
                    ["*.ogg", "*.mp3", "*.wav", "*.flac"])
                if not music_file:
                    raise SN2Error(f"{song.title}: audio file not found.")
                music_name, sel_name = f"MUSIC/{slot}.vig", f"SEL/{slot}_s.vig"
                pcm = sm2ddr.decode_audio(music_file, j["shift"])
                place.put(music_name,
                          pad(vig_encode(pcm, game.read(music_name)[:SECTOR])),
                          progress)
                start = song.sample_start if song.sample_start >= 0 else 30.0
                sample = sm2ddr.decode_audio(music_file, 0, start, 15.0, fade=True)
                place.put(sel_name,
                          pad(vig_encode(sample[:15 * 44100],
                                         game.read(sel_name)[:SECTOR])),
                          progress)
                set_record(game, slot, song, j["levels"])
            place.put(csq_name, pad(csq_data), progress)
            place.put(dt_name, pad(dt_data), progress)
            elf_lba, elf_size = game.files[game.elf_name.upper()]
            if len(game.elf) != elf_size:
                raise SN2Error("internal error: game program size changed")
            f.seek(elf_lba * SECTOR)
            f.write(bytes(game.elf))
            if place.finish():
                progress("The disc image grew to hold the longer songs")
    except BaseException:
        try:
            Path(out_iso).unlink()
        except OSError:
            pass
        raise
    progress(f"Done: {out_iso}")


def replace(game_folder, song_path, slot, out_iso, progress=print,
            unlock=False):
    """Replace one song (command-line use)."""
    if unlock:
        progress("--unlock-all is not used: songs are unlocked in the shop.")
    replace_many(game_folder, [(song_path, slot)], out_iso, progress)


def patch_iso(game, pieces, out_iso, progress):
    """Copy the original disc image and patch the changed data into it.

    Data that fits its old space is written in place. Larger data goes in the
    unused space after the last file on the disc; the game finds it through
    its own file table, which counts sectors from the start of the .DAT.
    """
    if Path(out_iso).resolve() == Path(game.iso).resolve():
        raise SN2Error("Choose a different output file; the original is kept.")
    end = max(lba + math.ceil(size / SECTOR) for lba, size in game.files.values())
    first_free = free = end + 16  # leave a little gap after the last file
    writes = []
    for name, data in pieces:
        x = game.entry(name)
        base = game.dat_lba(x)
        if len(data) <= x["size"]:
            data = data + b"\0" * (x["size"] - len(data))
            lba = base + x["sector"]
        else:
            count = math.ceil(len(data) / SECTOR)
            # UDF can keep an anchor 256 sectors before the end; stay clear
            if free < game.total and free + count > game.total - 260:
                free = game.total
            lba = free
            free += count
            progress(f"{name} is longer than before; stored in free space "
                     "at the end of the disc")
        writes.append((lba, data))
        game.set_entry(name, lba - base, data)
    elf_lba, elf_size = game.files[game.elf_name.upper()]
    if len(game.elf) != elf_size:
        raise SN2Error("internal error: game program size changed")
    writes.append((elf_lba, bytes(game.elf)))
    progress("Copying the disc image")
    shutil.copyfile(game.iso, out_iso)
    with open(out_iso, "r+b") as f:
        f.seek(0, 2)
        size = f.tell()
        need = free * SECTOR
        if free > first_free and need > size:
            f.write(b"\0" * (need - size))
            progress("The disc image grew to fit the song")
        for lba, data in writes:
            f.seek(lba * SECTOR)
            f.write(data)
    progress(f"Done: {out_iso}")


def find_ffmpeg():
    """Put ffmpeg on PATH if it sits next to this tool or a nearby folder."""
    import os
    if shutil.which("ffmpeg"):
        return
    here = Path(__file__).resolve().parent
    roots = [here, here.parent, Path.home() / "Downloads", Path.home() / "Desktop"]
    patterns = ["ffmpeg.exe", "*/ffmpeg.exe", "*/bin/ffmpeg.exe",
                "*/*/ffmpeg.exe", "*/*/bin/ffmpeg.exe"]
    for root in roots:
        for pattern in patterns:
            try:
                hit = next(root.glob(pattern), None)
            except OSError:
                hit = None
            if hit:
                os.environ["PATH"] = f"{hit.parent}{os.pathsep}{os.environ['PATH']}"
                return


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replace")
    r.add_argument("game")
    r.add_argument("song")
    r.add_argument("--slot", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--unlock-all", action="store_true",
                   help="make every song available from the start")
    u = sub.add_parser("unlock", help="patch a copy of the ISO so every "
                       "song is available from the start")
    u.add_argument("game")
    u.add_argument("--out", required=True)
    c = sub.add_parser("copy", help="write the game as an .iso with no changes")
    c.add_argument("game")
    c.add_argument("--out", required=True)
    s = sub.add_parser("songs")
    s.add_argument("game")
    args = p.parse_args(argv)
    find_ffmpeg()
    try:
        if args.cmd == "unlock":
            game = Game(args.game)
            if not game.iso:
                raise SN2Error("Give the original .iso for unlocking.")
            print("Unlocked from the start: " + ", ".join(game.unlock_all()))
            patch_iso(game, (), args.out, print)
        elif args.cmd == "copy":
            Game(args.game)
            write_iso(args.game, args.out, {}, progress=lambda m: None)
            print(f"Done: {args.out}")
        elif args.cmd == "songs":
            game = Game(args.game)
            for code, pos in sorted(game.records.items(),
                                    key=lambda kv: game.song_index(kv[0])):
                bpm = struct.unpack_from("<H", game.elf, pos + 32)[0]
                print(f"{game.song_index(code) + 1:3}  {code}  {bpm} BPM")
        else:
            replace(args.game, args.song, args.slot.lower(), args.out,
                    unlock=args.unlock_all)
    except (SN2Error, sm2ddr.ConvertError) as e:
        print("Error:", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

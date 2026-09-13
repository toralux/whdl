#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
whdl.py - bulk download Retroplay's WHDLoad packs from the Turran file server,
with chipset / language / memory filtering, resumable downloads and full/diff
modes. macOS, Linux, Windows - only the Python 3.8+ standard library.

How it works
  * Retroplay's Logiqx XML datfiles (name/size/CRC32 per pack) are the manifest.
  * Chipset / language / memory tags are derived from filenames, using the same
    markers as MrV2K's "WHDLoad Download Tool" (AGA, CD32, CDTV, _NTSC, _De, ...).
  * Downloads go over FTP by default (ftp2.grandis.nu, no transfer limits).
    HTTP fallback exists (--source http) but the server throttles HTTP and
    applies a ~500 files / 7 days limit there.
  * Files download to <name>.whdlpart and are renamed on success, so an
    interrupted run resumes exactly where it stopped.

Examples
  # Stock A500 set: ECS/OCS games, English only
  python3 whdl.py --dest ~/Amiga/WHDLoad --chipset ecs-ocs --lang en

  # Everything ECS/OCS, any language, sorted into AGA / ECS-OCS folders
  python3 whdl.py --dest ~/Amiga/WHDLoad --chipset ecs-ocs --layout chipset

  # AGA + CD32 games and demos, letter folders, CRC-verify everything present
  python3 whdl.py --dest ~/Amiga/WHDLoad --chipset aga,cd32 \\
      --type games,demos --layout letter --mode full --verify

  # Show what would download, without downloading
  python3 whdl.py --dest ~/Amiga/WHDLoad --chipset ecs-ocs --dry-run

If the WHDLoad ecosystem saves your retro life, donate to the Turran server
(see http://ftp2.grandis.nu - it is a community-funded machine).
"""

import argparse
import ftplib
import html
import io
import json
import os
import re
import socket
import sys
import time
import zipfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib

FTP_HOST = "ftp2.grandis.nu"
FTP_USER = "ftp"
FTP_PASS = "amiga"
HTTP_BASE = "http://ftp2.grandis.nu/turran/FTP"
PACKS_DIR = "Retroplay WHDLoad Packs"
PART_SUFFIX = ".whdlpart"
STATE_DIRNAME = ".whdl"
STATE_FILE = "state.json"
BLOCK = 1 << 16  # 64 KiB

CHIPSET_FOLDER = {
    "aga": "AGA", "ecs-ocs": "ECS-OCS", "cd32": "CD32",
    "cdtv": "CDTV", "cdrom": "CDROM", "ntsc": "NTSC",
}
# Chipset values describe the MINIMUM chipset a game needs. Retroplay's packs
# only mark AGA titles - everything unmarked runs on plain OCS and therefore
# on ECS and AGA machines too. 'ocs' and 'ecs' are aliases for that universal
# bucket ('ecs-ocs'); an ECS-only/OCS-only split does not exist in the data.
CHIPSET_ALIASES = {"ocs": "ecs-ocs", "ecs": "ecs-ocs"}
# Language markers in the exact order the WHDLoad Download Tool applies them
# (later matches win). Marker regex is  _<marker>(\.|_)  , case sensitive.
LANG_RULES = [
    ("Hr", "hr"), ("Cz", "cz"), ("De", "de"), ("Dk", "dk"), ("Es", "es"),
    ("Fi", "fi"), ("Fr", "fr"), ("Gr", "gr"), ("It", "it"), ("Nl", "nl"),
    ("Pl", "pl"), ("Se", "se"), ("DeFrIt", "multi"), ("DeEsFrIt", "multi"),
]
# Memory / variant markers (lowercased substring match), for --mem filtering.
MEM_MARKERS = ["512kb", "1mbchip", "15mb", "512k", "1mb", "2mb", "8mb",
               "12mb", "lowmem", "slow"]


# --------------------------------------------------------------------------- #
# Entry model
# --------------------------------------------------------------------------- #
class Entry(object):
    __slots__ = ("set_id", "machine", "name", "size", "crc", "chipset", "lang", "mem")

    def __init__(self, set_id, machine, name, size, crc):
        self.set_id = set_id
        self.machine = machine
        self.name = name
        self.size = size
        self.crc = crc
        self.chipset, self.lang, self.mem = tag_name(name)

    @property
    def key(self):
        return "%s/%s/%s" % (self.set_id, self.machine, self.name)


def tag_name(name):
    """Replicates the WHDLoad Download Tool's Scrape_Data() detection."""
    chipset = "ecs-ocs"
    if "_NTSC" in name:                      # NTSC is its own genre upstream
        chipset = "ntsc"
    elif "CD32" in name:
        chipset = "cd32"
    elif "CDTV" in name:
        chipset = "cdtv"
    elif "_CD" in name:
        chipset = "cdrom"
    elif "AGA" in name:
        chipset = "aga"

    lang = "en"
    for marker, code in LANG_RULES:
        if re.search(r"_(%s)(\.|_)" % marker, name):
            lang = code

    low = name.lower()
    mem = set()
    if "512k" in low and "512kb" not in low:
        mem.add("512k")
    for marker in MEM_MARKERS:
        if marker in low:
            mem.add(marker)
    if "_Chip" in name or "_Slow" in name:
        mem.add("chip")
    if "_Fast" in name:
        mem.add("fast")
    return chipset, lang, mem


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def human(nbytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024 or unit == "TB":
            return ("%.1f %s" if unit != "B" else "%d %s") % (nbytes, unit)
        nbytes /= 1024.0


def crc32_file(path):
    crc = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
    return "%08X" % crc


def http_get(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": "whdl.py/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def log(msg, quiet=False, newline=True):
    if quiet:
        return
    if newline:
        print(msg)
    else:
        sys.stdout.write(msg)
        sys.stdout.flush()


class Progress(object):
    """Single-line progress for TTYs; plain per-file lines otherwise."""

    def __init__(self, enabled):
        self.enabled = enabled and sys.stdout.isatty()

    def start(self, index, total, name, done_bytes, total_bytes):
        self.index, self.total, self.name = index, total, name
        self.done_bytes, self.total_bytes = done_bytes, total_bytes
        self.t0 = time.time()

    def update(self, added):
        self.done_bytes += added
        if not self.enabled:
            return
        pct = 100.0 * self.done_bytes / self.total_bytes if self.total_bytes else 100.0
        speed = self.done_bytes / max(time.time() - self.t0, 0.001)
        sys.stdout.write("\r\033[K[%d/%d] %5.1f%%  %s/s  %s" %
                         (self.index, self.total, pct, human(speed), self.name[:48]))
        sys.stdout.flush()

    def finish(self):
        if self.enabled:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Index (datfile) handling
# --------------------------------------------------------------------------- #
def set_id_from_stem(stem):
    s = stem.lower()
    beta = "beta" in s
    if "whdload" in s and "games" in s:
        return "games-beta" if beta else "games"
    if "whdload" in s and "demos" in s:
        return "demos-beta" if beta else "demos"
    if "magazines" in s:
        return "magazines"
    if "jst" in s:
        return "jst"
    if "hd loaders" in s:
        return "hdloaders"
    return None


def fetch_packs_root():
    url = HTTP_BASE + "/" + urllib.parse.quote(PACKS_DIR) + "/"
    return http_get(url).decode("utf-8", "replace")


def parse_root_index(root_html):
    """Returns (dats, dirs): dats = {set_id: (href_raw, date)}, dirs = {set_id: dir}."""
    hrefs = [html.unescape(m) for m in re.findall(r'href="([^"]+)"', root_html)
             if not m.startswith("/") and not m.startswith("?")]
    dats, dirs = {}, {}
    for href in hrefs:
        if href.endswith("/"):
            dirs[href.rstrip("/")] = True
        elif href.endswith(".zip") and re.search(r"\(\d{4}-\d{2}-\d{2}\)", href):
            stem = urllib.parse.unquote(href)[:-4]
            sid = set_id_from_stem(stem)
            date = re.search(r"\((\d{4}-\d{2}-\d{2})\)", stem).group(1)
            if sid and (sid not in dats or date > dats[sid][1]):
                dats[sid] = (href, date)
    # map set -> server dir name (dir hrefs equal zip stem with spaces->underscores)
    dirmap = {}
    for sid, (href, _date) in dats.items():
        stem = urllib.parse.unquote(href)[:-4]
        stem = re.sub(r"\s*\(\d{4}-\d{2}-\d{2}\)$", "", stem)  # drop " (date)"
        want = stem.replace(" ", "_")
        if want in dirs:
            dirmap[sid] = want
    return dats, dirmap


def parse_dat_zip(zip_bytes, set_id):
    entries = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if not info.filename.lower().endswith(".dat"):
                continue
            root = ET.fromstring(zf.read(info))
            for machine in root.iter("machine"):
                mname = machine.get("name", "")
                for rom in machine.iter("rom"):
                    name = rom.get("name")
                    size = rom.get("size")
                    crc = rom.get("crc")
                    if name and size and crc:
                        entries.append(Entry(set_id, mname, name, int(size), crc.upper()))
    return entries


def build_index(dest, want_sets, quiet):
    """Downloads the newest datfiles and returns (entries, dirmap)."""
    cache = os.path.join(dest, STATE_DIRNAME, "dats")
    os.makedirs(cache, exist_ok=True)
    for attempt in range(3):
        try:
            root_html = fetch_packs_root()
            break
        except Exception as ex:
            if attempt == 2:
                raise SystemExit("error: cannot reach %s: %s" % (HTTP_BASE, ex))
            time.sleep(3 * (attempt + 1))
    dats, dirmap = parse_root_index(root_html)

    entries = []
    for sid in want_sets:
        if sid not in dats:
            log("warning: no datfile found for set '%s' on the server" % sid, quiet)
            continue
        href, date = dats[sid]
        local = os.path.join(cache, urllib.parse.unquote(href))
        if not os.path.exists(local):
            url = HTTP_BASE + "/" + urllib.parse.quote(PACKS_DIR) + "/" + href
            log("index: downloading %s (%s)" % (urllib.parse.unquote(href), date), quiet)
            data = http_get(url)
            tmp = local + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, local)
        with open(local, "rb") as fh:
            entries.extend(parse_dat_zip(fh.read(), sid))
    return entries, dirmap


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #
def want_entry(e, args):
    if e.set_id not in args._sets:
        return False
    if args.chipset != ["all"] and e.chipset not in args.chipset:
        return False
    if args.lang and e.lang not in args.lang:
        return False
    if args.mem and not (e.mem & set(args.mem)):
        return False
    return True


def rel_path(e, args):
    parts = []
    if len(args._sets) > 1:
        parts.append(e.set_id)
    if args.layout == "letter":
        parts.append(e.machine)
    elif args.layout == "chipset":
        parts.append(CHIPSET_FOLDER[e.chipset])
    parts.append(e.name.replace("/", "-"))
    return os.path.join(*parts)


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #
def validate_part(part_path, entry, do_crc):
    """Returns 'ok', 'bad' or the byte offset from which to resume."""
    if not os.path.exists(part_path):
        return 0
    size = os.path.getsize(part_path)
    if size == entry.size:
        if do_crc and crc32_file(part_path) != entry.crc:
            os.remove(part_path)
            return "bad"
        return "ok"
    if size > entry.size:
        os.remove(part_path)
        return 0
    return size


class FtpSession(object):
    def __init__(self):
        self.ftp = None
        self.cwd_dir = None

    def connect(self):
        self.ftp = ftplib.FTP()
        self.ftp.encoding = "utf-8"
        self.ftp.connect(FTP_HOST, 21, timeout=90)
        self.ftp.login(FTP_USER, FTP_PASS)
        self.ftp.set_pasv(True)
        self.ftp.cwd(PACKS_DIR)
        self.cwd_dir = None

    def ensure_dir(self, subdir):
        want = subdir if subdir else ""
        if self.cwd_dir != want:
            if want:
                self.ftp.cwd(want)
            self.cwd_dir = want

    def fetch(self, entry, final_path, subdir, quiet, stats, prog, index, total):
        part_path = final_path + PART_SUFFIX
        for attempt in range(4):
            state = validate_part(part_path, entry, do_crc=False)
            if state == "ok":
                if crc32_file(part_path) == entry.crc:
                    os.replace(part_path, final_path)
                    return True
                os.remove(part_path)
                state = 0
            offset = 0 if state == "bad" else (state if isinstance(state, int) else 0)
            try:
                self.ensure_dir(subdir)
                prog.start(index, total, entry.name, offset, entry.size)
                mode = "ab" if offset else "wb"
                with open(part_path, mode) as fh:
                    cmd = "RETR " + entry.name
                    if offset:
                        self.ftp.retrbinary(cmd, lambda b: (fh.write(b), prog.update(len(b))),
                                            blocksize=BLOCK, rest=offset)
                    else:
                        self.ftp.retrbinary(cmd, lambda b: (fh.write(b), prog.update(len(b))),
                                            blocksize=BLOCK)
                prog.finish()
                if os.path.getsize(part_path) == entry.size:
                    if crc32_file(part_path) == entry.crc:
                        os.replace(part_path, final_path)
                        return True
                    log("crc mismatch, retrying: %s" % entry.name, quiet)
                    os.remove(part_path)
                else:
                    log("short read, resuming: %s" % entry.name, quiet)
            except (ftplib.Error, OSError, EOFError) as ex:
                prog.finish()
                log("ftp error (%s), retry %d/4: %s" % (ex, attempt + 1, entry.name), quiet)
                try:
                    self.ftp.quit()
                except Exception:
                    pass
                time.sleep(2 * (attempt + 1))
                self.connect()
        return False

    def quit(self):
        try:
            self.ftp.quit()
        except Exception:
            pass


def http_fetch(entry, url, final_path, quiet, stats, prog, index, total):
    part_path = final_path + PART_SUFFIX
    for attempt in range(4):
        state = validate_part(part_path, entry, do_crc=False)
        if state == "ok":
            if crc32_file(part_path) == entry.crc:
                os.replace(part_path, final_path)
                return True
            os.remove(part_path)
            state = 0
        offset = 0 if state == "bad" else (state if isinstance(state, int) else 0)
        headers = {"Range": "bytes=%d-" % offset} if offset else {}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=90) as resp:
                if offset and resp.status != 206:      # server ignored Range
                    offset = 0
                prog.start(index, total, entry.name, offset, entry.size)
                with open(part_path, "ab" if offset else "wb") as fh:
                    while True:
                        chunk = resp.read(BLOCK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        prog.update(len(chunk))
            prog.finish()
            if os.path.getsize(part_path) == entry.size:
                if crc32_file(part_path) == entry.crc:
                    os.replace(part_path, final_path)
                    return True
                log("crc mismatch, retrying: %s" % entry.name, quiet)
                os.remove(part_path)
            else:
                log("short read, retrying: %s" % entry.name, quiet)
        except Exception as ex:
            prog.finish()
            log("http error (%s), retry %d/4: %s" % (ex, attempt + 1, entry.name), quiet)
            time.sleep(2 * (attempt + 1))
    return False


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def local_ok(final_path, entry, verify):
    if not os.path.exists(final_path):
        return False
    if os.path.getsize(final_path) != entry.size:
        return False
    if verify and crc32_file(final_path) != entry.crc:
        return False
    return True


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Bulk-download Retroplay's WHDLoad packs with filters "
                    "(resumable; full/diff modes).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples")[1] if "Examples" in __doc__ else None)
    p.add_argument("--dest", required=True, help="output directory (created if missing)")
    p.add_argument("--chipset", default="floppy",
                   help="comma list, by MINIMUM chipset the game needs: "
                        "floppy (default) = disk games for standard Amigas "
                        "(ocs/ecs-ocs + aga, no CD titles), ocs / ecs / "
                        "ecs-ocs = runs on any chipset, aga = needs AGA, "
                        "cd32, cdtv, cdrom, ntsc, all = everything "
                        "[default: floppy]")
    p.add_argument("--lang", default="",
                   help="comma list of language codes to keep: en,de,fr,it,se,multi,... "
                        "[default: any]")
    p.add_argument("--mem", default="",
                   help="comma list of memory variants: 512k,512kb,1mb,2mb,8mb,12mb,"
                        "1.5mb,1mbchip,lowmem,slow,chip,fast [default: any]")
    p.add_argument("--type", default="games",
                   help="comma list of sets: games, demos, magazines, jst, hdloaders "
                        "[default: games]")
    p.add_argument("--include-beta", action="store_true",
                   help="also fetch the 'Beta & Unofficial' sets")
    p.add_argument("--mode", choices=("diff", "full"), default="diff",
                   help="diff = only new/changed since last run (+any missing); "
                        "full = check every file [default: diff]")
    p.add_argument("--verify", action="store_true",
                   help="CRC32-verify existing files (slower; with --mode full)")
    p.add_argument("--layout", choices=("flat", "letter", "chipset"), default="flat",
                   help="folder layout [default: flat]")
    p.add_argument("--source", choices=("ftp", "http"), default="ftp",
                   help="ftp has no transfer limits; http is throttled (~500 files/7d)")
    p.add_argument("--limit", type=int, default=0, help="download at most N files")
    p.add_argument("--dry-run", action="store_true",
                   help="show the plan (counts/bytes), download nothing")
    p.add_argument("--reset-state", action="store_true",
                   help="ignore previous state (diff acts like first run)")
    p.add_argument("--quiet", action="store_true", help="minimal output")
    args = p.parse_args(argv)

    def csv(value):
        return [v.strip().lower() for v in value.split(",") if v.strip()]

    expanded = []
    for c in (csv(args.chipset) or ["floppy"]):
        if c == "floppy":
            expanded += ["ecs-ocs", "aga"]
        else:
            expanded.append(CHIPSET_ALIASES.get(c, c))
    args.chipset = list(dict.fromkeys(expanded))
    args.lang = csv(args.lang)
    args.mem = csv(args.mem)
    bad_chip = set(args.chipset) - set(CHIPSET_FOLDER) - {"all"}
    if bad_chip:
        p.error("unknown --chipset value(s): %s" % ", ".join(sorted(bad_chip)))

    args._sets = set(csv(args.type))
    if args.include_beta:
        args._sets |= {s + "-beta" for s in args._sets if s in ("games", "demos")}
    unknown_sets = args._sets - {"games", "games-beta", "demos", "demos-beta",
                                 "magazines", "jst", "hdloaders"}
    if unknown_sets:
        p.error("unknown --type value(s): %s" % ", ".join(sorted(unknown_sets)))

    dest = os.path.expanduser(args.dest)
    os.makedirs(dest, exist_ok=True)
    state_path = os.path.join(dest, STATE_DIRNAME, STATE_FILE)

    # ---- index ---------------------------------------------------------- #
    entries, dirmap = build_index(dest, args._sets, args.quiet)
    if not entries:
        raise SystemExit("error: index is empty - nothing to do")
    log("index: %d entries across %d set(s)" % (len(entries), len(args._sets)),
        args.quiet)

    plan = [e for e in entries if want_entry(e, args)]
    plan.sort(key=lambda e: (e.set_id, e.machine, e.name))
    by_key = {e.key: e for e in entries}

    old_index = {}
    if os.path.exists(state_path) and not args.reset_state:
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                old_index = json.load(fh).get("index", {})
        except Exception:
            old_index = {}

    # ---- build the work list -------------------------------------------- #
    missing, reasons = [], {"new": 0, "changed": 0, "missing": 0, "badsize": 0, "badcrc": 0}
    for e in plan:
        path = os.path.join(dest, rel_path(e, args))
        if os.path.exists(path):
            if local_ok(path, e, verify=(args.mode == "full" and args.verify)):
                continue
            if os.path.getsize(path) != e.size:
                reasons["badsize"] += 1
            else:
                reasons["badcrc"] += 1
            missing.append(e)
            continue
        old_crc = old_index.get(e.key)
        if old_crc is None:
            reasons["new"] += 1
        elif old_crc != e.crc:
            reasons["changed"] += 1
        else:
            reasons["missing"] += 1
        missing.append(e)

    total_bytes = sum(e.size for e in missing)
    chipset_counts = {}
    for e in plan:
        chipset_counts[e.chipset] = chipset_counts.get(e.chipset, 0) + 1

    log("plan: %d file(s) match the filter (%s)" %
        (len(plan), ", ".join("%s=%d" % kv for kv in sorted(chipset_counts.items()))),
        args.quiet)
    if args.mode == "diff":
        log("diff: %d new, %d changed, %d missing, %d wrong-size, %d bad-crc -> "
            "%d file(s) to fetch (%s)" %
            (reasons["new"], reasons["changed"], reasons["missing"],
             reasons["badsize"], reasons["badcrc"], len(missing), human(total_bytes)),
            args.quiet)
    else:
        log("full: %d file(s) to fetch (%s)" % (len(missing), human(total_bytes)),
            args.quiet)

    if args.dry_run:
        for e in missing[:25]:
            log("  %s" % rel_path(e, args), args.quiet)
        if len(missing) > 25:
            log("  ... and %d more" % (len(missing) - 25), args.quiet)
        return 0

    if args.source == "http" and len(missing) > 400:
        log("warning: HTTP source is limited to ~500 files / 7 days on this "
            "server - use --source ftp for bulk runs", args.quiet)

    if not missing:
        log("nothing to download.", args.quiet)
        with open(state_path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"index": {e.key: e.crc for e in entries}}, fh)
        os.replace(state_path + ".tmp", state_path)
        return 0

    if args.limit and args.limit < len(missing):
        missing = missing[:args.limit]
        log("--limit: fetching only the first %d file(s)" % args.limit, args.quiet)

    # ---- download -------------------------------------------------------- #
    stats = {"downloaded": 0, "bytes": 0, "failed": 0}
    failed = []
    prog = Progress(not args.quiet)
    started = time.time()
    try:
        if args.source == "ftp":
            ftp = FtpSession()
            ftp.connect()
            cur_subdir = None
            for i, e in enumerate(missing, 1):
                final_path = os.path.join(dest, rel_path(e, args))
                os.makedirs(os.path.dirname(final_path), exist_ok=True)
                subdir = "/".join(x for x in (dirmap.get(e.set_id, ""), e.machine) if x)
                ok = ftp.fetch(e, final_path, subdir, args.quiet, stats, prog, i,
                               len(missing))
                if ok:
                    stats["downloaded"] += 1
                    stats["bytes"] += e.size
                else:
                    stats["failed"] += 1
                    failed.append(e)
            ftp.quit()
        else:
            base = HTTP_BASE + "/" + urllib.parse.quote(PACKS_DIR)
            for i, e in enumerate(missing, 1):
                final_path = os.path.join(dest, rel_path(e, args))
                os.makedirs(os.path.dirname(final_path), exist_ok=True)
                url = "%s/%s/%s" % (base, urllib.parse.quote(dirmap.get(e.set_id, "")),
                                    "/".join(urllib.parse.quote(x)
                                             for x in (e.machine, e.name)))
                if http_fetch(e, url, final_path, args.quiet, stats, prog, i,
                              len(missing)):
                    stats["downloaded"] += 1
                    stats["bytes"] += e.size
                else:
                    stats["failed"] += 1
                    failed.append(e)
    except KeyboardInterrupt:
        print("\ninterrupted - state saved; run the same command again to resume.")
        _save_state(state_path, entries)
        return 130

    if failed and not args.quiet:
        log("failed downloads (%d):" % len(failed))
        for e in failed[:20]:
            log("  %s" % rel_path(e, args))
        if len(failed) > 20:
            log("  ... and %d more" % (len(failed) - 20))

    elapsed = max(time.time() - started, 0.1)
    log("finished: %d downloaded (%s), %d failed, %.1f files/s, %s in %dm%02ds" %
        (stats["downloaded"], human(stats["bytes"]), stats["failed"],
         stats["downloaded"] / elapsed, human(stats["bytes"]),
         int(elapsed) // 60, int(elapsed) % 60), args.quiet)

    _save_state(state_path, entries)
    return 1 if failed else 0


def _save_state(state_path, entries):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": 1,
                   "index": {e.key: e.crc for e in entries}}, fh)
    os.replace(tmp, state_path)


if __name__ == "__main__":
    sys.exit(main())

# whdl — WHDLoad bulk fetcher

Single-file, stdlib-only Python 3.8+ tool that bulk-downloads Retroplay's WHDLoad
packs from the Turran file server with chipset / language / memory filtering,
resumable downloads, and full/diff modes. Runs on macOS, Linux and Windows —
no pip installs, no Wine, no VM.

## Install

Requires Python 3.8+ (macOS ships `python3`; if missing, run `xcode-select --install`).

```bash
git clone https://github.com/toralux/whdl.git
cd whdl
python3 whdl.py --help
```

Or grab the single file directly:

```bash
curl -LO https://raw.githubusercontent.com/toralux/whdl/main/whdl.py
python3 whdl.py --help
```

## Quick start: stock A500 (OCS, English)

    python3 whdl.py --dest ~/Amiga/WHDLoad --chipset ocs --lang en

First run downloads the whole filtered set (about 2,700 files / 2.1 GB with
`--lang en`; 3,329 files / 3.1 GB without it). After that, every run only
fetches **new or changed** packs (diff mode) — re-run monthly to stay current.

## Options

| Flag | Values | Default | Meaning |
|---|---|---|---|
| `--dest` | path | *(required)* | Output directory (created if missing) |
| `--chipset` | `floppy` (default), `ocs` / `ecs` / `ecs-ocs`, `aga`, `cd32`, `cdtv`, `cdrom`, `ntsc`, `all` | `floppy` | Filter by MINIMUM chipset the game needs (comma list). `floppy` = disk games for standard Amigas (ocs + aga); CD32/CDTV/CD-ROM and NTSC titles are skipped unless named explicitly |
| `--lang` | `en, de, fr, it, se, dk, es, fi, gr, nl, pl, cz, hr, multi` | any | Keep only these languages |
| `--mem` | `512k, 512kb, 1mb, 2mb, 8mb, 12mb, 1.5mb, 1mbchip, lowmem, slow, chip, fast` | any | Pre-installed memory variants |
| `--type` | `games, demos, magazines, jst, hdloaders` | `games` | Which sets to fetch |
| `--include-beta` | flag | off | Also the "Beta & Unofficial" sets |
| `--mode` | `diff, full` | `diff` | diff = new/changed since last run + missing; full = check everything |
| `--verify` | flag | off | With `full`: CRC32-verify existing files and re-download corruption |
| `--layout` | `flat, letter, chipset` | `flat` | `letter` = 0-Z folders; `chipset` = AGA / ECS-OCS folders |
| `--source` | `ftp, http` | `ftp` | FTP has no limits; HTTP is throttled (~500 files / 7 days) |
| `--limit N` | number | 0 | Only fetch first N (testing) |
| `--dry-run` | flag | off | Show plan and sizes, download nothing |
| `--reset-state` | flag | off | Forget what's been seen (diff acts like first run) |

Filter values express the **minimum chipset a game requires**:

- `aga` — the game requires an AGA machine (A1200/A4000/CD32) and will not run on a stock A500.
- `ocs`, `ecs`, `ecs-ocs` — the game runs on plain OCS, i.e. on any Amiga. All three names select the same bucket, which Retroplay labels "ECS-OCS".
- `floppy` (the default) — `ocs + aga`: every disk-based game a standard Amiga can run. CD32, CDTV, CD-ROM and NTSC titles are excluded unless named explicitly, e.g. `--chipset floppy,cd32` or `--chipset all`.

Retroplay's datfiles only mark AGA (plus CD32/CDTV/CD-ROM/NTSC) variants; there are no ECS-only or OCS-only markers in the data, so a finer split is not possible — unmarked games form the universal bucket. A stock A500 therefore plays everything selected by `--chipset ocs`.

## Examples

    # Everything ECS/OCS in AGA/ECS-OCS sorted folders
    python3 whdl.py --dest ~/Amiga/WHDLoad --chipset ecs-ocs --layout chipset

    # AGA + CD32 games and demos, letter folders
    python3 whdl.py --dest ~/Amiga/WHDLoad --chipset aga,cd32 --type games,demos --layout letter

    # Just 1 MB chip-mem variants for an expanded A500
    python3 whdl.py --dest ~/Amiga/WHDLoad --chipset ecs-ocs --mem 1mb

    # See what a filter matches before committing
    python3 whdl.py --dest ~/Amiga/WHDLoad --chipset ecs-ocs --lang en --dry-run

## How it behaves

- **Manifest**: Retroplay's XML datfiles (fetched fresh each run) define the
  exact file set with size + CRC32 — same database as the WHDLoad Download Tool.
- **Resume**: files download to `name.whdlpart` and are renamed only after size
  + CRC32 check. Interrupted (Ctrl-C) or killed runs continue where they
  stopped — just run the same command again.
- **Diff mode** remembers the last seen index in `DEST/.whdl/state.json`, so a
  re-run only downloads new releases or updated versions of packs you track.
- **Integrity**: every download is CRC32-checked against the dat before it is
  accepted; `--mode full --verify` also audits files you already have.
- FTP login, path layout and the filename→chipset/language tagging mirror
  MrV2K's WHDLoad Download Tool so results match its filters.

## Credits

- **Inspiration:** [MrV2K's WHDLoad Download Tool](https://github.com/MrV2K/WHDLoad-Download-Tool) —
  the excellent GUI downloader this tool reimplements as a scriptable,
  cross-platform alternative (same datfiles, same filename-based
  chipset / language / memory filter logic).
- **Game data:** [Retroplay's WHDLoad packs](https://eab.abime.net/showthread.php?t=61028),
  served by the community-run [Turran FTP](http://ftp2.grandis.nu) —
  please donate to keep it running.
- **WHDLoad** itself and the WHDLoad installer community: [whdload.de](https://whdload.de).

If the set saves your retro life, donate to the Turran server — it's a
community-funded machine: http://ftp2.grandis.nu

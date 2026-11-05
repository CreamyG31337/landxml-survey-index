# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10"]  # optional: only for the map's orthophoto background
# ///
"""
LandXML Survey Index — indexes geospatial survey files and writes a self-contained
sortable/pageable HTML report (or plain text with --output txt).

File types indexed: LandXML (.xml) surfaces & alignments, LAZ/LAS point clouds,
scans recorded in Trimble JobXML (.jxl) files, GeoTIFF orthophotos, TBC project
files (.vce), and CSV point files (.csv).

For each CSV point file (--batch) the report shows the row/column count, spatial
extent, and the distinct feature codes it contains — type a code into the tab's
filter box to find every CSV that uses it. Each run also writes a footprints file
(where every file's data lies) beside the report, for boundary and map searches.

Project-specific settings — folder layout, which folder holds the control list,
the codes your crews use for control — live in survey_index.toml next to this
script (see survey_index.example.toml), or a file named with --config. Without
one, generic defaults apply.

File discovery uses Everything (voidtools HTTP API) when available, and falls back
to a directory walk automatically if Everything is not installed.

Deduplication (all active by default):
  Hash dedup:     identical files (same MD5 prefix) collapse to the most canonical
                  copy; a ×N count is shown.
  Path priority:  canonical path chosen by the config's folder-preference order
                  ([paths] priority). Add to it with --prefer-path FRAGMENT
                  (repeatable, prepended).
  Content dedup:  surfaces/alignments with the same name AND geometry counts across
                  different files are treated as one entry (versioned if they differ).
                  Disable with --no-content-dedup.

Usage:
  uv run survey_index.py --path "C:\\Projects\\MyProject"
  uv run survey_index.py --path "C:\\Projects\\MyProject" --batch
  uv run survey_index.py --path "C:\\Projects\\MyProject" --title "My Project"
  uv run survey_index.py --path "C:\\Projects\\MyProject" --output-file "D:\\reports\\index.html"
  uv run survey_index.py --path "C:\\Projects\\MyProject" --logo "C:\\logo.png"
  uv run survey_index.py --path "C:\\Projects\\MyProject" --sort size
  uv run survey_index.py --path "C:\\Projects\\MyProject" --output txt

Scheduled indexing (run_index.bat + Windows Task Scheduler):
  schtasks /create /tn "Survey Index" /tr "C:\\Projects\\landxml-survey-index\\run_index.bat"
           /sc weekly /d MON /st 07:00 /f
"""

import argparse
import base64
import hashlib
import json
import math
import os
import re
import struct
import tomllib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PAGE_SIZE = 50


# ── configuration ─────────────────────────────────────────────────────────────
# Built-in defaults are deliberately generic. Anything tied to one company's
# folder layout or one crew's coding habits belongs in the config file
# (survey_index.toml beside this script, or --config FILE); see
# survey_index.example.toml for every setting, documented. A setting given in the
# file replaces the default below outright (lists are not merged).

CONFIG_FILE = Path(__file__).with_name("survey_index.toml")

DEFAULT_CONFIG: dict[str, dict] = {
    "paths": {
        # Folder-name fragments, most preferred first, used to pick which copy of a
        # duplicated file the report shows. Empty = no preference.
        "priority": [],
        # Only index CSVs whose path contains this fragment, or any of a list of
        # fragments ("" or [] = anywhere).
        "csv_under": "",
        # CSVs whose path contains this fragment are the project control list
        # ("" = none; control is then recognised by code/name only).
        "control_under": "",
    },
    "footprints": {
        # Grid cell size in metres for surface/CSV footprints.
        "cell_size": 5.0,
        # A CSV point this close (metres) to a control-list point is control.
        "control_tolerance": 0.10,
        # Horizontal reach (metres) for drawing a JXL scan's footprint when its
        # scan frame doesn't record the distance it was defined at.
        "scan_reach": 40.0,
    },
    "map": {
        # Orthophoto drawn behind the Map tab: an image with a world file beside it
        # (.jpg + .jgw, .png + .pgw, .tif + .tfw, or .wld), or a .zip holding both.
        # A path or glob pattern, relative to --path unless absolute; the newest
        # match is used. "" = no background.
        "basemap": "",
        # Coarsest ground resolution (metres per pixel) for the background tiles.
        # The image is decoded at full, ½, ¼ or ⅛ scale — the coarsest that's at
        # least this detailed — so a 2.8 cm drone ortho becomes 11 cm tiles.
        # Finer settings cost memory: a ½-scale decode of a gigapixel ortho needs ~2 GB.
        "basemap_resolution": 0.15,
    },
    "control": {
        # Words that mark a point *code* as control/setup rather than as-built
        # data. Matched as whole words, case-insensitive, trailing digits allowed
        # (MON matches MON2); spaces match any whitespace; "_" reads as a space.
        "code_words": ["MON", "MONUMENT", "BM", "BENCHMARK", "BENCH MARK", "CP",
                       "CTRL", "CONTROL", "IRON BAR", "IRON PIN", "IB", "BRASS CAP",
                       "BRASS PLUG", "BRASS DISC", "NAIL", "PK", "SPIKE", "TARGET"],
        # Codes that mean control only when they are the whole code, optionally
        # with "-<number>" (TEST, TEST-001 — but not DENSITY-TEST).
        "whole_codes": ["TEST"],
        # Point-*name* prefixes that mark control, when followed by a digit, a
        # space, "-", "_" or nothing (CP matches CP1 and "CP 2", not CPOND).
        "name_prefixes": ["CP", "MON", "BM", "IB", "CTRL"],
    },
}

# Set from the config by apply_config(); module-level so the parsers below can
# use them without threading settings through every call.
FOOTPRINT_CELL = DEFAULT_CONFIG["footprints"]["cell_size"]
CONTROL_TOL = DEFAULT_CONFIG["footprints"]["control_tolerance"]
SCAN_REACH = DEFAULT_CONFIG["footprints"]["scan_reach"]
_CTRL_CODE: re.Pattern | None = None
_CTRL_NAME: re.Pattern | None = None


def load_config(path: Path | None) -> dict:
    """DEFAULT_CONFIG overlaid with the TOML file at `path` (if given), key by key."""
    cfg = {section: dict(values) for section, values in DEFAULT_CONFIG.items()}
    if path is None:
        return cfg
    # utf-8-sig: Notepad and PowerShell 5.1 often save with a BOM, which tomllib rejects.
    try:
        user = tomllib.loads(Path(path).read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path}: {e}") from None
    for section, values in user.items():
        if section not in cfg or not isinstance(values, dict):
            raise ValueError(f"{path}: unknown section [{section}]")
        for key, value in values.items():
            if key not in cfg[section]:
                raise ValueError(f"{path}: unknown setting '{key}' in [{section}]")
            cfg[section][key] = value
    return cfg


def _alternation(items: list[str]) -> str:
    """Regex alternation of literal words, where a space matches any whitespace."""
    return "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in items)


def apply_config(cfg: dict) -> None:
    """Install the footprint and control-recognition settings from a loaded config."""
    global FOOTPRINT_CELL, CONTROL_TOL, SCAN_REACH, _CTRL_CODE, _CTRL_NAME
    FOOTPRINT_CELL = float(cfg["footprints"]["cell_size"])
    CONTROL_TOL = float(cfg["footprints"]["control_tolerance"])
    SCAN_REACH = float(cfg["footprints"]["scan_reach"])
    ctl = cfg["control"]
    parts = []
    if ctl["code_words"]:
        parts.append(rf"\b(?:{_alternation(ctl['code_words'])})\d*\b")
    if ctl["whole_codes"]:
        parts.append(rf"^\s*(?:{_alternation(ctl['whole_codes'])})(?:-\d+)?\s*$")
    _CTRL_CODE = re.compile("|".join(parts), re.I) if parts else None
    _CTRL_NAME = (re.compile(rf"^(?:{_alternation(ctl['name_prefixes'])})(?:\d|[\s_-]|$)", re.I)
                  if ctl["name_prefixes"] else None)


apply_config(DEFAULT_CONFIG)


# ── data access ───────────────────────────────────────────────────────────────

def query_everything(search: str, url: str = "http://localhost", page_size: int = 2000) -> list[dict]:
    """Return every match, paging through the API so large result sets aren't truncated."""
    results: list[dict] = []
    while True:
        params = urllib.parse.urlencode({
            "s": search, "j": "1", "path_column": "1",
            "offset": str(len(results)), "count": str(page_size),
        })
        with urllib.request.urlopen(f"{url}/?{params}", timeout=10) as r:
            data = json.loads(r.read().decode())
        page = data["results"]
        results.extend(page)
        if not page or len(results) >= data["totalResults"]:
            return results


def scan_files(search_path: str, ext: str) -> list[dict]:
    """Walk directory tree; return [{path, name}] matching Everything's format."""
    results = []
    for root, _dirs, files in os.walk(search_path):
        for fname in files:
            if fname.lower().endswith("." + ext.lower()):
                results.append({"path": root, "name": fname})
    return results


_everything_ok: bool | None = None  # None = untested, True = working, False = unavailable


def find_files(search_path: str, ext: str, everything_url: str = "http://localhost") -> list[dict]:
    """Query Everything if available; fall back to os.walk silently after first failure."""
    global _everything_ok
    if _everything_ok is not False:
        try:
            results = query_everything(f'ext:{ext} path:"{search_path}"', everything_url)
            _everything_ok = True
            return results
        except Exception:
            if _everything_ok is None:
                print("  [info] Everything HTTP API not reachable — using directory scan (slower).")
            _everything_ok = False
    return scan_files(search_path, ext)


def surface_point_count(el, pfx: str) -> int:
    """Vertex count for a Surface element.

    Triangulated (TIN) surfaces store vertices as <P> elements — count those.
    Surfaces defined only by <SourceData> (breaklines/boundaries/contours) have
    no <P>/<F>; their geometry lives in <PntList3D> ("x y z …") and <PntList2D>
    ("x y …") coordinate lists, so fall back to counting those vertices instead.
    """
    p = sum(1 for _ in el.iter(f"{pfx}P"))
    if p:
        return p
    total = 0
    for pl in el.iter(f"{pfx}PntList3D"):
        if pl.text:
            total += len(pl.text.split()) // 3
    for pl in el.iter(f"{pfx}PntList2D"):
        if pl.text:
            total += len(pl.text.split()) // 2
    return total


def surface_grid(el, pfx: str) -> dict:
    """Footprint grid for a Surface element (see grid_add). LandXML lists
    coordinates northing first: "N E Z" in <P>/<PntList3D>, "N E" in <PntList2D>.
    Like surface_point_count, falls back to the source-data lists when there's no TIN.
    Malformed coordinates are skipped rather than failing the whole file."""
    grid: dict = {}

    def add(n: str, e: str) -> None:
        try:
            grid_add(grid, float(e), float(n))
        except ValueError:
            pass

    for p in el.iter(f"{pfx}P"):
        v = (p.text or "").split()
        if len(v) >= 2:
            add(v[0], v[1])
    if grid:
        return grid
    for tag, stride in (("PntList3D", 3), ("PntList2D", 2)):
        for pl in el.iter(f"{pfx}{tag}"):
            v = (pl.text or "").split()
            for k in range(0, len(v) - 1, stride):
                add(v[k], v[k + 1])
    return grid


def alignment_line(el, pfx: str) -> list[list[float]]:
    """Plan polyline [[E, N], …] of an Alignment's horizontal geometry (<CoordGeom>),
    for drawing on the report's map. Lines are drawn by their ends, curves sampled
    about their centre every ~2°, spirals by their ends — fine at site scale.
    Segments whose points are only references (pntRef, no coordinates) are skipped."""
    cg = el.find(f"{pfx}CoordGeom")
    if cg is None:
        return []

    def pt(node):
        v = (node.text or "").split() if node is not None else []
        return (float(v[1]), float(v[0])) if len(v) >= 2 else None  # "N E" → (E, N)

    out: list[tuple[float, float]] = []
    for seg in cg:
        try:
            a, b = pt(seg.find(f"{pfx}Start")), pt(seg.find(f"{pfx}End"))
            c = pt(seg.find(f"{pfx}Center"))
        except ValueError:
            continue
        if a is None or b is None:
            continue
        pts = [a, b]
        if seg.tag.endswith("Curve") and c is not None:
            r = math.hypot(a[0] - c[0], a[1] - c[1])
            a0 = math.atan2(a[1] - c[1], a[0] - c[0])
            sweep = math.atan2(b[1] - c[1], b[0] - c[0]) - a0
            if seg.get("rot") == "cw":
                while sweep > 0:
                    sweep -= 2 * math.pi
            else:
                while sweep < 0:
                    sweep += 2 * math.pi
            n = max(2, int(abs(sweep) / math.radians(2)))
            pts = [(c[0] + r * math.cos(a0 + sweep * k / n), c[1] + r * math.sin(a0 + sweep * k / n))
                   for k in range(n + 1)]
        if out and math.dist(out[-1], pts[0]) < 0.01:
            pts = pts[1:]
        out.extend(pts)
    return [[round(e, 2), round(n, 2)] for e, n in out]


def parse_file(
    filepath: str, tags: list[str], max_download_bytes: int = 0
) -> tuple[dict[str, list[tuple[str, int]]], str | None]:
    """Read file once; return ({tag: [(name, pts, faces)]}, md5). Non-LandXML → ({}, None).
    When Surfaces are indexed, the dict also carries "_grids": {surface name:
    footprint grid} for the footprints file; when Alignments are, "_lines":
    {alignment name: plan polyline} for the map.

    Cloud-only (OneDrive placeholder) files are read — which makes OneDrive download
    them — only if no larger than max_download_bytes; otherwise they are skipped,
    since their contents can't be known without downloading.
    """
    try:
        if not is_local(filepath) and Path(filepath).stat().st_size > max_download_bytes:
            return {}, None
        content = Path(filepath).read_bytes()
        md5 = hashlib.md5(content).hexdigest()
        root = ET.fromstring(content)
        local = root.tag.split("}")[-1] if "}" in root.tag else root.tag
        if local != "LandXML":
            return {}, None
        ns  = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""
        pfx = f"{{{ns}}}" if ns else ""
        out = {}
        for tag in tags:
            items = [
                (el.get("name", "<unnamed>"),
                 surface_point_count(el, pfx),
                 sum(1 for _ in el.iter(f"{pfx}F")),
                 float(el.get("length", 0) or 0),
                 float(el.get("staStart", 0) or 0))
                for el in root.iter(f"{pfx}{tag}")
            ]
            if items:
                out[tag] = items
        if "Surface" in out:
            grids = {}
            for el in root.iter(f"{pfx}Surface"):
                g = surface_grid(el, pfx)
                if g:
                    grids.setdefault(el.get("name", "<unnamed>"), g)
            if grids:
                out["_grids"] = grids
        if "Alignment" in out:
            lines = {}
            for el in root.iter(f"{pfx}Alignment"):
                line = alignment_line(el, pfx)
                if len(line) >= 2:
                    lines.setdefault(el.get("name", "<unnamed>"), line)
            if lines:
                out["_lines"] = lines
        return out, md5
    except Exception:
        return {}, None


def file_mtime(path: str) -> float:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def fmt_date(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d")


_CLOUD_ATTRS = 0x1000 | 0x00040000 | 0x00400000  # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS

def is_local(path: str) -> bool:
    """Return False if this is an OneDrive cloud-only file (not downloaded locally)."""
    try:
        return not bool(Path(path).stat().st_file_attributes & _CLOUD_ATTRS)
    except (OSError, AttributeError):
        return True


def parse_laz_header(filepath: str) -> dict | None:
    """Read LAS/LAZ header; return {"pts": N, "density": pts/m², "bbox": (xmin,
    ymin, xmax, ymax) or None} or None. The header extent covers every point,
    stray ones included, so it can be larger than the real scan coverage."""
    try:
        with open(filepath, "rb") as f:
            hdr = f.read(270)
        if len(hdr) < 108 or hdr[:4] != b"LASF":
            return None
        ver_major, ver_minor = hdr[24], hdr[25]
        legacy_pts = struct.unpack_from("<I", hdr, 107)[0]
        if ver_major == 1 and ver_minor >= 4 and len(hdr) >= 255:
            pts = struct.unpack_from("<Q", hdr, 247)[0] or legacy_pts
        else:
            pts = legacy_pts
        density = 0.0
        bbox = None
        if len(hdr) >= 211:
            xmax, xmin, ymax, ymin = struct.unpack_from("<4d", hdr, 179)
            area = (xmax - xmin) * (ymax - ymin)
            if area > 0:
                bbox = (xmin, ymin, xmax, ymax)
                if pts > 0:
                    density = pts / area
        return {"pts": pts, "density": density, "bbox": bbox}
    except Exception:
        return None


def partial_hash(filepath: str, nbytes: int = 65536) -> str | None:
    """MD5 of first nbytes — fast proxy for large binary files."""
    try:
        with open(filepath, "rb") as f:
            return hashlib.md5(f.read(nbytes)).hexdigest()
    except Exception:
        return None


def parse_tiff_header(filepath: str) -> dict | None:
    """Read TIFF/BigTIFF IFD for dimensions and GeoTIFF spatial tags (33550+33922 or 34264)."""
    import math as _math
    _empty = {"width": 0, "height": 0, "gsd": 0.0, "bbox": None}
    try:
        with open(filepath, "rb") as f:
            hdr = f.read(16)
            if len(hdr) < 8 or hdr[:2] not in (b"II", b"MM"):
                return None
            e = "<" if hdr[:2] == b"II" else ">"
            magic = struct.unpack_from(e + "H", hdr, 2)[0]
            if magic == 43:
                # BigTIFF: 8-byte offsets, 20-byte IFD entries, count is uint64
                if len(hdr) < 16:
                    return _empty.copy()
                ifd_off = struct.unpack_from(e + "Q", hdr, 8)[0]
                f.seek(ifd_off)
                n = struct.unpack_from(e + "Q", f.read(8), 0)[0]
                ifd = f.read(n * 20)
                entry_size, cnt_fmt, val_fmt, vo = 20, e + "Q", e + "Q", 12
                val_inline_types = {3: e+"H", 4: e+"I", 16: e+"Q"}
            elif magic == 42:
                # Classic TIFF: 4-byte offsets, 12-byte IFD entries
                ifd_off = struct.unpack_from(e + "I", hdr, 4)[0]
                f.seek(ifd_off)
                n_raw = f.read(2)
                if len(n_raw) < 2:
                    return _empty.copy()
                n = struct.unpack_from(e + "H", n_raw, 0)[0]
                ifd = f.read(n * 12)
                entry_size, cnt_fmt, val_fmt, vo = 12, e + "I", e + "I", 8
                val_inline_types = {3: e+"H", 4: e+"I"}
            else:
                return None

            width = height = 0
            scale_off = scale_cnt = tp_off = tp_cnt = 0
            xform_off = xform_cnt = 0

            for i in range(n):
                ep = i * entry_size
                if ep + entry_size > len(ifd):
                    break
                tag = struct.unpack_from(e + "H", ifd, ep)[0]
                typ = struct.unpack_from(e + "H", ifd, ep + 2)[0]
                cnt = struct.unpack_from(cnt_fmt, ifd, ep + 4)[0]
                if tag in (256, 257):
                    val = struct.unpack_from(val_inline_types.get(typ, val_fmt), ifd, ep + vo)[0]
                    if tag == 256: width = val
                    else:          height = val
                elif tag == 33550 and typ == 12:
                    scale_off, scale_cnt = struct.unpack_from(val_fmt, ifd, ep + vo)[0], cnt
                elif tag == 33922 and typ == 12:
                    tp_off, tp_cnt = struct.unpack_from(val_fmt, ifd, ep + vo)[0], cnt
                elif tag == 34264 and typ == 12 and cnt == 16:
                    xform_off = struct.unpack_from(val_fmt, ifd, ep + vo)[0]

            gsd = 0.0
            bbox = None

            if scale_off and scale_cnt >= 1:
                f.seek(scale_off)
                sd = f.read(scale_cnt * 8)
                if len(sd) >= 8:
                    gsd = struct.unpack_from(e + "d", sd, 0)[0]
                if tp_off and tp_cnt >= 6:
                    f.seek(tp_off)
                    td = f.read(tp_cnt * 8)
                    if len(td) >= 48:
                        x0 = struct.unpack_from(e + "d", td, 24)[0]
                        y0 = struct.unpack_from(e + "d", td, 32)[0]
                        if gsd and width and height:
                            bbox = (x0, y0 - height * gsd, x0 + width * gsd, y0)
            elif xform_off:
                f.seek(xform_off)
                m = struct.unpack_from(e + "16d", f.read(128))
                a, b_, d_ = m[0], m[1], m[3]
                e_, f_, h_ = m[4], m[5], m[7]
                gsd = _math.sqrt(a * a + e_ * e_)
                if gsd and width and height:
                    corners_x = [d_, d_ + a*(width-1), d_ + b_*(height-1), d_ + a*(width-1) + b_*(height-1)]
                    corners_y = [h_, h_ + e_*(width-1), h_ + f_*(height-1), h_ + e_*(width-1) + f_*(height-1)]
                    bbox = (min(corners_x), min(corners_y), max(corners_x), max(corners_y))

        return {"width": width, "height": height, "gsd": gsd, "bbox": bbox}
    except Exception:
        return None


# Header tokens that identify columns in a survey CSV.
_CSV_EAST = {"easting", "east", "x", "e"}
_CSV_NORTH = {"northing", "north", "y", "n"}
_CSV_ELEV = {"elevation", "elev", "height", "ortho height", "ellipsoid height", "z"}
_CSV_CODE = {"code", "codes", "feature", "featurecode", "feature code", "featcode",
             "feat", "fc", "fcode", "string", "desc", "description", "layer",
             "attribute", "attr", "note", "notes", "remark", "remarks", "comment",
             "comments", "point code", "pointcode", "pcode", "pt code", "ptcode"}
_CSV_NAME = {"point", "point name", "pointname", "point id", "point number", "pt",
             "pt#", "pnt", "name", "id"}
_CSV_MAX_CODES = 300  # cap distinct codes per file (safety on pathological data)
_CSV_COL_CARD = 60    # a text column with more distinct values than this is treated
                      # as free-text / point IDs and excluded from the search index
_CSV_MAX_TERMS = 400  # overall cap on searchable text tokens per file

_EMPTY_CSV = {"rows": 0, "cols": 0, "bbox": None, "zmin": None, "zmax": None,
              "codes": [], "terms": [], "cells": [], "excluded": 0}


def _ctrl_key(e: float, n: float) -> tuple[int, int]:
    return round(e / CONTROL_TOL), round(n / CONTROL_TOL)


def near_control(control: set, e: float, n: float) -> bool:
    """True if (e, n) is within about CONTROL_TOL of a known control point."""
    i, j = _ctrl_key(e, n)
    return any((i + a, j + b) in control for a in (-1, 0, 1) for b in (-1, 0, 1))


def load_control(paths: list[str]) -> set:
    """Snapped coordinates of every point in the project's control CSVs."""
    pts: list[tuple[float, float]] = []
    for p in paths:
        parse_csv_meta(p, points=pts)
    return {_ctrl_key(e, n) for e, n in pts}


def parse_csv_meta(filepath: str, control: set | None = None,
                   points: list | None = None) -> dict | None:
    """Read a CSV survey/point file.

    Returns {"rows": data-row count, "cols": column count, "bbox": (Emin, Nmin,
    Emax, Nmax) or None, "zmin"/"zmax": elevation range or None, "codes": sorted
    distinct feature codes, "terms": sorted distinct text values across all
    low-cardinality columns (the full-content search index), "cells": footprint
    grid (see grid_cells), "excluded": points left out of extent and footprint as
    control}. Coordinate columns come from a header row naming them (e.g.
    Northing/Easting/Elevation); a headerless file is placed by _guess_enz.
    A point is control if its code or name matches the config's [control] lists
    (see apply_config), or it lies on a coordinate in `control` (see
    load_control). Blank codes never count as control: plenty of real topo is
    uncoded. If `points` is given, every point's (E, N) is appended to it.
    Feature codes come from a header-named code column when
    present; otherwise from the trailing (description) field, as in a headerless
    PNEZD export — keeping only non-numeric values so coordinate columns are never
    mistaken for codes. "terms" makes the report's filter box match on file content
    (codes, descriptions, layers) rather than filename alone; high-cardinality
    columns like point IDs are excluded. Returns None if the file can't be read.
    Streams line-by-line, so memory stays flat on large exports.
    """
    import csv as _csv
    try:
        with open(filepath, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            sample = f.read(16384)
            if not sample.strip():
                return dict(_EMPTY_CSV)
            try:
                delim = _csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
            except _csv.Error:
                delim = ","

            f.seek(0)
            reader = _csv.reader(f, delimiter=delim)
            first = next(reader, None)
            if first is None:
                return dict(_EMPTY_CSV)
            cols = len(first)

            # Header detection: peek a handful of data rows and compare how many
            # numeric fields the first row has against the data rows. A header row
            # has fewer (its coordinate columns are text). This is deterministic —
            # csv.Sniffer().has_header() is unreliable on survey exports.
            peek: list[list[str]] = []
            for row in reader:
                if row:
                    peek.append(row)
                if len(peek) >= 8:
                    break
            # A header has at most one numeric field: a first data row whose point
            # name is text ("S2", "0+015") above numerically-named rows is not one.
            first_numeric = _numcount(first)
            data_numeric = max((_numcount(r) for r in peek), default=0)
            has_header = (first_numeric < data_numeric and first_numeric <= 1) if peek \
                else (first_numeric == 0)

            e_i = n_i = z_i = c_i = p_i = None
            if has_header:
                for i, raw in enumerate(first):
                    key = raw.strip().lower()
                    if   e_i is None and key in _CSV_EAST:  e_i = i
                    elif n_i is None and key in _CSV_NORTH: n_i = i
                    elif z_i is None and key in _CSV_ELEV:  z_i = i
                    elif c_i is None and key in _CSV_CODE:  c_i = i
                    elif p_i is None and key in _CSV_NAME:  p_i = i
            else:
                e_i, n_i, z_i = _guess_enz([first] + peek)
                if 0 not in (e_i, n_i, z_i):
                    p_i = 0  # P of PNEZD
            code_named = c_i is not None
            if c_i is None and cols:
                c_i = cols - 1  # fall back to the trailing (description) field

            emin = nmin = zmin = float("inf")
            emax = nmax = zmax = float("-inf")
            have_en = e_i is not None and n_i is not None
            codes: set[str] = set()
            grid: dict = {}
            excluded = 0
            # Distinct text values per column, for full-content search. Columns that
            # exceed _CSV_COL_CARD distinct values (point IDs, free-text notes) are
            # dropped — they bloat the index and aren't useful filter terms.
            col_text: dict[int, set[str]] = {}
            dropped: set[int] = set()

            def is_control(row: list[str], e: float, n: float) -> bool:
                if (_CTRL_CODE and c_i is not None and c_i < len(row)
                        and _CTRL_CODE.search(row[c_i].replace("_", " "))):
                    return True
                if (_CTRL_NAME and p_i is not None and p_i < len(row)
                        and _CTRL_NAME.match(row[p_i].strip())):
                    return True
                return bool(control) and near_control(control, e, n)

            def consume(row: list[str]) -> None:
                nonlocal emin, emax, nmin, nmax, zmin, zmax, excluded
                if have_en and max(e_i, n_i) < len(row):
                    try:
                        e, n = float(row[e_i]), float(row[n_i])
                        if math.isfinite(e) and math.isfinite(n):
                            if points is not None:
                                points.append((e, n))
                            if is_control(row, e, n):
                                excluded += 1
                            else:
                                emin, emax = min(emin, e), max(emax, e)
                                nmin, nmax = min(nmin, n), max(nmax, n)
                                grid_add(grid, e, n)
                    except ValueError:
                        pass
                if z_i is not None and z_i < len(row):
                    try:
                        z = float(row[z_i])
                        zmin, zmax = min(zmin, z), max(zmax, z)
                    except ValueError:
                        pass
                if c_i is not None and c_i < len(row) and len(codes) < _CSV_MAX_CODES:
                    v = row[c_i].strip()
                    # A name-matched code column is trusted verbatim; the trailing
                    # fallback keeps only non-numeric text (so Z values aren't codes).
                    if v and (code_named or not _is_number(v)):
                        codes.add(v)
                for i, cell in enumerate(row):
                    if i in dropped:
                        continue
                    v = cell.strip()
                    if not v or _is_number(v):
                        continue
                    bucket = col_text.setdefault(i, set())
                    if v not in bucket:
                        if len(bucket) >= _CSV_COL_CARD:
                            dropped.add(i)      # high-cardinality column → drop it
                            col_text[i] = set()  # free the memory
                        else:
                            bucket.add(v)

            rows = 0
            if not has_header:            # the row we consumed is data, not a header
                rows += 1
                consume(first)
            for row in peek:              # buffered data rows from header detection
                rows += 1
                consume(row)
            for row in reader:            # remaining data rows
                if not row:
                    continue
                rows += 1
                consume(row)

            bbox = (emin, nmin, emax, nmax) if have_en and emin != float("inf") else None
            zr = (zmin, zmax) if z_i is not None and zmin != float("inf") else (None, None)
            terms: set[str] = set()
            for i, bucket in col_text.items():
                if i in dropped:
                    continue
                terms |= bucket
            return {"rows": rows, "cols": cols, "bbox": bbox,
                    "zmin": zr[0], "zmax": zr[1], "codes": sorted(codes),
                    "terms": sorted(terms)[:_CSV_MAX_TERMS], "cells": grid_cells(grid),
                    "excluded": excluded}
    except Exception:
        return None


def _guess_enz(rows: list[list[str]]) -> tuple[int | None, int | None, int | None]:
    """(east, north, elev) column indices for a headerless CSV, or Nones.

    UTM-sized values identify the columns directly — northings have 7 digits,
    eastings 6 — whatever order the export used. (UTM eastings are always
    166,000–834,000 m; northings reach 7 digits beyond ~9° from the equator in
    either hemisphere.) Otherwise — site grids, state plane, anything else —
    assume the common PNEZD layout. Elevation is taken as the column after the
    later of the two, if numeric.
    """
    cols = min(len(r) for r in rows)
    vals: dict[int, list[float]] = {}
    for i in range(cols):
        try:
            vals[i] = [float(r[i]) for r in rows]
        except ValueError:
            continue
    north = [i for i, v in vals.items() if all(1e6 <= x < 1e7 for x in v)]
    east  = [i for i, v in vals.items() if all(1e5 <= x < 1e6 for x in v)]
    if north and east:
        n_i = north[0]
        e_i = min(east, key=lambda i: abs(i - n_i))  # point numbers can be 6 digits too
    elif cols >= 4 and 1 in vals and 2 in vals:
        n_i, e_i = 1, 2
    else:
        return None, None, None
    z = max(e_i, n_i) + 1
    return e_i, n_i, (z if z in vals else None)


def _xml_float(el, tag: str) -> float | None:
    try:
        return float(el.findtext(tag))
    except (TypeError, ValueError):
        return None


def parse_jxl_scans(filepath: str) -> list[dict] | None:
    """Scans recorded in a Trimble JobXML (.jxl) file, located without their point data.

    Each <ScanningRecord> points at the instrument station it was taken from
    (StationID → StationRecord → the station point's grid coordinates), the
    backsight that orients the instrument (BackBearingID → OrientationCorrection;
    grid azimuth = horizontal circle + correction) and its scan frame
    (ScanFrameID → ScanningFrameRecord: the frame's vertices as horizontal circle
    / zenith angle pairs, defined at AtDistance metres).

    The footprint is the convex hull of the station and each frame vertex projected
    onto the plan at that distance (SCAN_REACH when the frame has none). It shows
    where the scan was pointed, not exactly where its points landed: checked
    against a LAZ export's header extent, the edges agreed to within ~5 m.

    Returns [{"name", "station", "points", "time", "raw", "xy": (E, N) | None,
    "poly": [[E, N], …] | None}] for scans not marked deleted, or None if the file
    isn't readable JobXML.
    """
    try:
        root = ET.parse(filepath).getroot()
    except Exception:
        return None
    fb = root.find("FieldBook")
    if fb is None:
        return None
    records = list(fb)
    by_id = {r.get("ID"): r for r in records}

    # Grid coordinates by point name; later records win, and the <Reductions>
    # section (the job's final computed coordinates) overrides the field book.
    grid: dict[str, tuple[float, float]] = {}
    reductions = root.find("Reductions")
    for source in (fb.iter("PointRecord"), reductions if reductions is not None else []):
        for p in source:
            if p.findtext("Deleted") == "true":
                continue
            g = p.find("Grid")
            if g is None:
                g = p.find("ComputedGrid")
            if g is None:
                continue
            n, e = _xml_float(g, "North"), _xml_float(g, "East")
            if n is not None and e is not None:
                grid[p.findtext("Name")] = (e, n)

    scans = []
    for rec in records:
        if rec.tag != "ScanningRecord" or rec.findtext("Deleted") == "true":
            continue
        station = by_id.get(rec.findtext("StationID"))
        backsight = by_id.get(rec.findtext("BackBearingID"))
        frame = by_id.get(rec.findtext("ScanFrameID"))
        st_name = station.findtext("StationName") if station is not None else None
        xy = grid.get(st_name)
        orient = _xml_float(backsight, "OrientationCorrection") if backsight is not None else None
        poly = None
        if xy and orient is not None and frame is not None:
            reach = _xml_float(frame, "AtDistance") or SCAN_REACH
            pts = [xy]
            for v in frame.iter("Vertex"):
                hc, za = _xml_float(v, "HorizontalCircle"), _xml_float(v, "VerticalCircle")
                if hc is None or za is None:
                    continue
                az, hd = math.radians(hc + orient), reach * abs(math.sin(math.radians(za)))
                pts.append((xy[0] + hd * math.sin(az), xy[1] + hd * math.cos(az)))
            if len(pts) > 2:
                poly = [[round(e, 2), round(n, 2)] for e, n in convex_hull(pts)]
        scans.append({
            "name": rec.findtext("Name") or "<unnamed>",
            "station": st_name or "",
            "points": int(_xml_float(rec, "NumberOfPoints") or 0),
            "time": rec.get("TimeStamp") or "",
            "raw": rec.findtext("FileName") or "",
            "xy": xy,
            "poly": poly,
        })
    return scans


def convex_hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Convex hull, counter-clockwise (Andrew's monotone chain)."""
    pts = sorted(set(pts))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    upper: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _numcount(row: list[str]) -> int:
    """Count fields in a row that parse as numbers (used for header detection)."""
    return sum(1 for v in row if _is_number(v.strip()))


# ── footprints ────────────────────────────────────────────────────────────────

def grid_add(grid: dict, e: float, n: float) -> None:
    """Count a point into its FOOTPRINT_CELL-sized grid cell, keyed (column, row).
    A stray point (control, a far-off check shot) only adds one isolated cell,
    rather than stretching the extent the way it does a bounding box."""
    if math.isfinite(e) and math.isfinite(n):
        key = (int(e // FOOTPRINT_CELL), int(n // FOOTPRINT_CELL))
        grid[key] = grid.get(key, 0) + 1


def grid_cells(grid: dict) -> list[list[int]]:
    """Serialisable form of a grid: [[column, row, count], …], row-major."""
    return [[i, j, c] for (i, j), c in sorted(grid.items(), key=lambda kv: (kv[0][1], kv[0][0]))]


def cells_bbox(cells: list[list[int]]) -> tuple | None:
    """Extent (Emin, Nmin, Emax, Nmax) of a cell list, to cell precision."""
    if not cells:
        return None
    c = FOOTPRINT_CELL
    return (min(i for i, _, _ in cells) * c, min(j for _, j, _ in cells) * c,
            (max(i for i, _, _ in cells) + 1) * c, (max(j for _, j, _ in cells) + 1) * c)


def build_footprints(rows_by_tag: dict[str, list[dict]]) -> list[dict]:
    """Where each indexed surface/file lies, for boundary and map queries.
    Surfaces and CSVs carry occupied grid cells with point counts (CSVs also how
    many control points were left out); scans carry an approximate footprint
    polygon and their instrument station; point clouds and orthophotos only have
    their header bounding box."""
    kinds = {"Surface": "surface", "CSV": "csv", "Scan": "scan",
             "PointCloud": "pointcloud", "Ortho": "ortho"}
    out = []
    for tag, kind in kinds.items():
        for r in rows_by_tag.get(tag, []):
            cells = r.get("cells") or []
            bbox = r.get("bbox") or cells_bbox(cells)
            if not bbox:
                continue
            fp = {"type": kind, "name": r["name"], "path": r["filepath"], "date": r["date"],
                  "bbox": [round(v, 3) for v in bbox]}
            if cells:
                fp["cells"] = cells
            if r.get("excluded"):
                fp["excluded"] = r["excluded"]
            if r.get("poly"):
                fp["poly"] = r["poly"]
                fp["station"] = [round(v, 3) for v in r["station_xy"]]
                fp["job"] = r["job"]
                fp["pts"] = r["pts"]
            out.append(fp)
    return out


# Footprints larger than this many cells are drawn on the report's map with
# merged cells (2×2, 4×4, …) to keep the report small; the footprints file keeps
# full resolution.
MAP_MAX_CELLS = 1500


def _coarsen(cells: list[list[int]], k: int) -> list[list[int]]:
    """Merge cells into k×k blocks, summing point counts."""
    merged: dict[tuple[int, int], int] = {}
    for i, j, c in cells:
        key = (i // k, j // k)
        merged[key] = merged.get(key, 0) + c
    return [[i, j, c] for (i, j), c in merged.items()]


def _delta_cells(cells: list[list[int]]) -> list[int]:
    """Row-major cells as a flat [Δrow, col, count, …] list: Δrow is the step from
    the previous cell's row, and col is relative to the previous cell's column
    when Δrow is 0 (usually 1), absolute otherwise. Small numbers → small report."""
    out: list[int] = []
    pi = pj = 0
    for i, j, c in sorted(cells, key=lambda x: (x[1], x[0])):
        dj = j - pj
        out += [dj, i - pi if dj == 0 else i, c]
        pi, pj = i, j
    return out


BASEMAP_TILE = 1024  # px; large tiles keep the file count down in synced folders
_WORLD_EXTS = (".jgw", ".jpgw", ".pgw", ".pngw", ".tfw", ".tifw", ".wld")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff")


def find_basemap(pattern: str, search_path: str) -> Path | None:
    """Newest file matching the config's basemap path/glob (relative to search_path)."""
    import glob as _glob
    full = pattern if os.path.isabs(pattern) else os.path.join(search_path, pattern)
    hits = [Path(p) for p in _glob.glob(full, recursive=True) if os.path.isfile(p)]
    return max(hits, key=file_mtime) if hits else None


def build_basemap(source: Path, out_dir: Path, resolution: float) -> dict | None:
    """Cut a georeferenced orthophoto into map tiles in out_dir; return their layout.

    The image is placed by its world file (six lines: pixel width, two rotation
    terms, negative pixel height, and the centre of the top-left pixel). It's
    decoded at the coarsest of full, ½, ¼ or ⅛ scale that's still at least as
    detailed as `resolution` — JPEG decodes straight to that scale, so even a
    gigapixel drone ortho takes seconds and modest memory (other formats are
    decoded in full first, so prefer JPEG for big images). Tiles are a pyramid of
    BASEMAP_TILE-px JPEGs named <level>_<x>_<y>.jpg, level 0 the most detailed.
    Tiles are reused while the source file, its size/date and the resolution are
    unchanged. Needs Pillow; returns None (with a message) if it's missing or the
    source can't be used.
    """
    try:
        from PIL import Image
    except ImportError:
        print("  [info] Map background skipped: Pillow isn't installed "
              "(`uv run survey_index.py …` installs it automatically).")
        return None
    Image.MAX_IMAGE_PIXELS = None  # drone orthos are legitimately gigapixel

    stat = source.stat()
    key = {"source": str(source), "mtime": stat.st_mtime, "size": stat.st_size,
           "resolution": resolution}
    meta_path = out_dir / "basemap.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if all(meta.get(k) == v for k, v in key.items()) and (out_dir / "0_0_0.jpg").exists():
            return meta
    except (OSError, ValueError):
        pass

    zf = None
    try:
        if source.suffix.lower() == ".zip":
            import zipfile
            zf = zipfile.ZipFile(source)
            names = zf.namelist()
            img_name = next((n for n in names if n.lower().endswith(_IMAGE_EXTS)), None)
            wld_name = next((n for n in names if n.lower().endswith(_WORLD_EXTS)), None)
            if not img_name or not wld_name:
                print(f"  [warn] Map background: {source.name} has no image + world file pair.")
                return None
            world = zf.read(wld_name).decode("ascii", "replace")
            img_file = zf.open(img_name)
        else:
            wld = next((source.with_suffix(ext) for ext in _WORLD_EXTS
                        if source.with_suffix(ext).exists()), None)
            if wld is None:
                print(f"  [warn] Map background: no world file (.jgw/.tfw/.wld…) beside {source.name}.")
                return None
            world = wld.read_text(encoding="ascii", errors="replace")
            img_file = open(source, "rb")
        A, D, B, E, C, F = [float(v) for v in world.split()[:6]]
        if D or B:
            print(f"  [warn] Map background: {source.name} is rotated; only north-up images are supported.")
            return None

        print(f"  Cutting map background tiles from {source.name}…")
        with img_file:
            im = Image.open(img_file)
            W, H = im.size
            scale = 1
            while scale < 8 and abs(A) * scale * 2 <= resolution:
                scale *= 2
            im.draft("RGB", (W // scale, H // scale))  # JPEG: decode at reduced scale
            im = im.convert("RGB")
            target_w = max(1, round(W * abs(A) / resolution))
            if im.size[0] > target_w * 1.5:  # formats without draft decoding
                im = im.resize((target_w, max(1, round(im.size[1] * target_w / im.size[0]))), Image.LANCZOS)
    except Exception as ex:
        print(f"  [warn] Map background: couldn't read {source.name}: {ex}")
        return None
    finally:
        if zf is not None:
            zf.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*_*_*.jpg"):
        old.unlink()
    levels = []
    T = BASEMAP_TILE
    while True:
        w, h = im.size
        levels.append([w, h])
        lv = len(levels) - 1
        for ty in range((h + T - 1) // T):
            for tx in range((w + T - 1) // T):
                im.crop((tx * T, ty * T, min(w, (tx + 1) * T), min(h, (ty + 1) * T))) \
                  .save(out_dir / f"{lv}_{tx}_{ty}.jpg", quality=80)
        if w <= T and h <= T:
            break
        im = im.resize(((w + 1) // 2, (h + 1) // 2), Image.LANCZOS)

    meta = dict(key, name=source.name,
                x0=C - A / 2, y0=F - E / 2,          # top-left corner of the image
                gsd=abs(A) * W / levels[0][0],       # metres per level-0 pixel
                tile=T, levels=levels)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    n_tiles = sum(((w + T - 1) // T) * ((h + T - 1) // T) for w, h in levels)
    print(f"  Map background: {n_tiles} tiles at {meta['gsd']:.2f} m/px in {out_dir}")
    return meta


def build_map_data(rows_by_tag: dict[str, list[dict]], control: set) -> dict:
    """Compact copy of the footprints, embedded in the report for its Map tab.

    Coordinates are stored relative to an origin "o" near the middle of the site
    (a multiple of 64 cells, so merged cells still line up) to keep numbers short:
    plan coordinates to 0.1 m; grid cells delta-encoded (see _delta_cells) in
    units of the file's cell size, cell × "k". Each file: "t" type (s surface,
    c CSV, n scan, p point cloud, o orthophoto, a alignment), "n" name, "d" date,
    "p" path, plus its geometry: "c" cells, "g" scan polygon and "st" station,
    "b" bounding box, or "l" alignment line. "k" lists control points.
    """
    kinds = [("Surface", "s"), ("CSV", "c"), ("Scan", "n"), ("PointCloud", "p"),
             ("Ortho", "o"), ("Alignment", "a")]
    centres = []
    for tag, _ in kinds:
        for r in rows_by_tag.get(tag, []):
            bb = r.get("bbox") or cells_bbox(r.get("cells") or [])
            if bb:
                centres.append(((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2))
    block = FOOTPRINT_CELL * 64
    if centres:
        es, ns = sorted(c[0] for c in centres), sorted(c[1] for c in centres)
        mid = (es[len(es) // 2], ns[len(ns) // 2])  # median, so stray files don't move it
    else:
        mid = (0.0, 0.0)
    oe, on = math.floor(mid[0] / block) * block, math.floor(mid[1] / block) * block
    oi, oj = round(oe / FOOTPRINT_CELL), round(on / FOOTPRINT_CELL)

    def rel(pts):
        return [v for e, n in pts for v in (round(e - oe, 1), round(n - on, 1))]

    files = []
    for tag, t in kinds:
        for r in rows_by_tag.get(tag, []):
            f = {"t": t, "n": r["name"], "d": r["date"], "p": r["filepath"]}
            cells = r.get("cells") or []
            if cells:
                k = 1
                while len(cells) > MAP_MAX_CELLS:
                    k *= 2
                    cells = _coarsen(r["cells"], k)
                f["k"] = k
                f["c"] = _delta_cells([[i - oi // k, j - oj // k, c] for i, j, c in cells])
                if r.get("excluded"):
                    f["x"] = r["excluded"]
            elif r.get("poly"):
                f["g"] = rel(r["poly"])
                f["st"] = rel([r["station_xy"]])
                f["q"] = r["pts"]
            elif r.get("line"):
                f["l"] = rel(r["line"])
            elif r.get("bbox"):
                b = r["bbox"]
                f["b"] = rel([(b[0], b[1]), (b[2], b[3])])
            else:
                continue
            files.append(f)
    ctrl = sorted({(i * CONTROL_TOL, j * CONTROL_TOL) for i, j in control})
    return {"cell": FOOTPRINT_CELL, "o": [oe, on], "f": files, "k": rel(ctrl)}


# ── dedup helpers ─────────────────────────────────────────────────────────────

def priority_score(path: str, priority: list[str]) -> int:
    p = path.lower()
    for i, frag in enumerate(priority):
        if frag.lower() in p:
            return i
    return len(priority)


def best_path(paths: list[str], priority: list[str]) -> str:
    return min(paths, key=lambda p: priority_score(p, priority))


def folder_url(filepath: str) -> str:
    """Convert a Windows path to a file:// URI for the containing folder."""
    parts = Path(filepath).parent.parts  # ('C:\\', 'Users', …)
    if parts and parts[0].endswith("\\"):
        drive = parts[0].rstrip("\\")
        rest  = "/".join(urllib.parse.quote(p, safe="") for p in parts[1:])
        return f"file:///{drive}/{rest}/"
    encoded = urllib.parse.quote(str(Path(filepath).parent).replace("\\", "/"), safe="/:")
    return f"file://{encoded}/"


# ── pipeline ──────────────────────────────────────────────────────────────────

def build_rows(
    raw: list[tuple[str, dict, float, str | None]],
    tag: str,
    priority: list[str],
    no_hash_dedup: bool,
    no_content_dedup: bool,
) -> list[dict]:
    """Full dedup pipeline for one element type → list of table-row dicts."""

    # Filter to files that contain this tag
    relevant = [(p, info[tag], t, md5) for p, info, t, md5 in raw if tag in info]

    # Phase 2: hash-based file dedup
    if not no_hash_dedup:
        groups: dict[str, list] = defaultdict(list)
        no_hash = []
        for path, surfaces, t, md5 in relevant:
            if md5:
                groups[md5].append((path, surfaces, t))
            else:
                no_hash.append((path, surfaces, t, 1))
        files: list[tuple[str, list, float, int]] = list(no_hash)
        for group in groups.values():
            paths = [p for p, _, _ in group]
            canon = best_path(paths, priority)
            canon_surfs, canon_t = next((s, t) for p, s, t in group if p == canon)
            files.append((canon, canon_surfs, canon_t, len(group)))
    else:
        files = [(p, s, t, 1) for p, s, t, _ in relevant]

    # name → [(path, pts, faces, length, sta_start, mtime, copies)]
    name_map: dict[str, list] = defaultdict(list)
    for path, surfaces, t, copies in files:
        for name, pts, faces, length, sta_start in surfaces:
            name_map[name].append((path, pts, faces, length, sta_start, t, copies))

    rows: list[dict] = []

    if not no_content_dedup:
        # Phase 1: group by (pts, faces, length) — same geometry = same element
        for name, entries in name_map.items():
            by_geom: dict[tuple, list] = defaultdict(list)
            for path, pts, faces, length, sta_start, t, copies in entries:
                by_geom[(pts, faces, length)].append((path, sta_start, t, copies))

            n_versions = len(by_geom)
            for vi, ((pts, faces, length), pt_entries) in enumerate(
                sorted(by_geom.items(), key=lambda kv: kv[0][0] or kv[0][1] or kv[0][2], reverse=True), 1
            ):
                total_copies = sum(c for _, _, _, c in pt_entries)
                canon = best_path([p for p, _, _, _ in pt_entries], priority)
                canon_t   = next(t   for p, _, t, _ in pt_entries if p == canon)
                canon_sta = next(sta for p, sta, _, _ in pt_entries if p == canon)
                rows.append({
                    "name":         name,
                    "pts":          pts,
                    "faces":        faces,
                    "length":       length,
                    "sta_start":    canon_sta,
                    "copies":       total_copies,
                    "n_files":      len(pt_entries),
                    "n_versions":   n_versions,
                    "version_note": f"v{vi}/{n_versions}" if n_versions > 1 else "",
                    "date":         fmt_date(canon_t),
                    "filename":     Path(canon).name,
                    "filepath":     canon,
                    "folder_url":   folder_url(canon),
                })
    else:
        for name, entries in name_map.items():
            total_copies = sum(c for _, _, _, _, _, _, c in entries)
            max_pts      = max(pts    for _, pts, _, _, _, _, _ in entries)
            max_faces    = max(faces  for _, _, faces, _, _, _, _ in entries)
            max_len      = max(ln     for _, _, _, ln, _, _, _ in entries)
            canon        = best_path([p for p, _, _, _, _, _, _ in entries], priority)
            canon_t      = next(t   for p, _, _, _, _, t, _ in entries if p == canon)
            canon_sta    = next(sta for p, _, _, _, sta, _, _ in entries if p == canon)
            rows.append({
                "name":         name,
                "pts":          max_pts,
                "faces":        max_faces,
                "length":       max_len,
                "sta_start":    canon_sta,
                "copies":       total_copies,
                "n_files":      len(entries),
                "n_versions":   1,
                "version_note": "",
                "date":         fmt_date(canon_t),
                "filename":     Path(canon).name,
                "filepath":     canon,
                "folder_url":   folder_url(canon),
            })

    return rows


def sort_rows(rows: list[dict], sort_key: str) -> list[dict]:
    if sort_key == "size":
        return sorted(rows, key=lambda r: r.get("size", r.get("pts", 0)), reverse=True)
    if sort_key == "date":
        return sorted(rows, key=lambda r: r["date"])
    return sorted(rows, key=lambda r: r["name"].casefold())


def build_laz_rows(results: list[dict], priority: list[str]) -> list[dict]:
    """Index LAZ/LAS files: hash-dedup by first 64 KB, path priority."""
    hash_groups: dict[str, list] = defaultdict(list)
    no_hash: list = []

    for item in results:
        path = str(Path(item["path"]) / item["name"])
        try:
            stat = Path(path).stat()
            mtime, size = stat.st_mtime, stat.st_size
        except OSError:
            continue
        local = is_local(path)
        header = parse_laz_header(path) if local else None
        pts = header["pts"] if header else 0
        density = header["density"] if header else 0.0
        bbox = header["bbox"] if header else None
        h = partial_hash(path) if local else None
        if h:
            hash_groups[h].append((path, pts, density, bbox, mtime, size))
        else:
            no_hash.append((path, pts, density, bbox, mtime, size, 1))

    files: list[tuple] = list(no_hash)
    for group in hash_groups.values():
        paths = [e[0] for e in group]
        canon = best_path(paths, priority)
        files.append((*next(e for e in group if e[0] == canon), len(group)))

    rows = []
    for path, pts, density, bbox, mtime, size, copies in files:
        rows.append({
            "name":         Path(path).stem,
            "pts":          pts,
            "density":      density,
            "bbox":         bbox,
            "size":         size,
            "copies":       copies,
            "version_note": "",
            "date":         fmt_date(mtime),
            "filename":     Path(path).name,
            "filepath":     path,
            "folder_url":   folder_url(path),
        })
    return rows


def build_ortho_rows(results: list[dict], priority: list[str]) -> list[dict]:
    """Index TIFF/GeoTIFF orthophotos: hash-dedup by first 64 KB, path priority."""
    hash_groups: dict[str, list] = defaultdict(list)
    no_hash: list = []

    for item in results:
        path = str(Path(item["path"]) / item["name"])
        try:
            stat = Path(path).stat()
            mtime, size = stat.st_mtime, stat.st_size
        except OSError:
            continue
        local = is_local(path)
        hdr = parse_tiff_header(path) if local else {"width": 0, "height": 0, "gsd": 0.0, "bbox": None}
        if hdr is None:
            continue  # not a valid TIFF
        gsd, bbox = hdr["gsd"], hdr["bbox"]
        if not gsd and local:  # no embedded GeoTIFF tags — try sidecar .tfw world file
            tfw = Path(path).with_suffix(".tfw")
            if tfw.exists():
                try:
                    A, D, B, E, C, F = [float(x) for x in tfw.read_text().split()][:6]
                    gsd = (A**2 + D**2) ** 0.5
                    w, h2 = hdr["width"], hdr["height"]
                    if w and h2:
                        xs = [C, C+A*(w-1), C+B*(h2-1), C+A*(w-1)+B*(h2-1)]
                        ys = [F, F+D*(w-1), F+E*(h2-1), F+D*(w-1)+E*(h2-1)]
                        bbox = (min(xs), min(ys), max(xs), max(ys))
                except Exception:
                    pass
        h = partial_hash(path) if local else None
        entry = (path, hdr["width"], hdr["height"], gsd, bbox, mtime, size)
        if h:
            hash_groups[h].append(entry)
        else:
            no_hash.append((*entry, 1))

    files: list[tuple] = list(no_hash)
    for group in hash_groups.values():
        paths = [p for p, *_ in group]
        canon = best_path(paths, priority)
        _, w, h, gsd, bbox, t, sz = next(e for e in group if e[0] == canon)
        files.append((canon, w, h, gsd, bbox, t, sz, len(group)))

    rows = []
    for path, width, height, gsd, bbox, mtime, size, copies in files:
        rows.append({
            "name":         Path(path).stem,
            "width":        width,
            "height":       height,
            "gsd":          gsd,
            "bbox":         bbox,
            "size":         size,
            "copies":       copies,
            "version_note": "",
            "date":         fmt_date(mtime),
            "filename":     Path(path).name,
            "filepath":     path,
            "folder_url":   folder_url(path),
        })
    return rows


def build_vce_rows(results: list[dict], priority: list[str]) -> list[dict]:
    """Index TBC project files (.vce): hash-dedup by first 64 KB, path priority."""
    hash_groups: dict[str, list] = defaultdict(list)
    no_hash: list = []

    for item in results:
        path = str(Path(item["path"]) / item["name"])
        try:
            stat = Path(path).stat()
            mtime, size = stat.st_mtime, stat.st_size
        except OSError:
            continue
        local = is_local(path)
        h = partial_hash(path) if local else None
        if h:
            hash_groups[h].append((path, mtime, size))
        else:
            no_hash.append((path, mtime, size, 1))

    files: list[tuple] = list(no_hash)
    for group in hash_groups.values():
        paths = [p for p, _, _ in group]
        canon = best_path(paths, priority)
        _, canon_t, canon_sz = next(e for e in group if e[0] == canon)
        files.append((canon, canon_t, canon_sz, len(group)))

    rows = []
    for path, mtime, size, copies in files:
        rows.append({
            "name":         Path(path).stem,
            "size":         size,
            "copies":       copies,
            "version_note": "",
            "date":         fmt_date(mtime),
            "filename":     Path(path).name,
            "filepath":     path,
            "folder_url":   folder_url(path),
        })
    return rows


def build_csv_rows(results: list[dict], priority: list[str], control: set | None = None) -> list[dict]:
    """Index CSV point files: row/column counts, spatial extent and footprint with
    control points left out (see parse_csv_meta); hash-dedup by first 64 KB, path priority."""
    hash_groups: dict[str, list] = defaultdict(list)
    no_hash: list = []

    for item in results:
        path = str(Path(item["path"]) / item["name"])
        try:
            stat = Path(path).stat()
            mtime, size = stat.st_mtime, stat.st_size
        except OSError:
            continue
        # Unlike the LAZ/TIFF/VCE indexers, CSV content search needs the actual
        # file contents, so we parse every file — including OneDrive online-only
        # placeholders. Opening one triggers Files On-Demand to hydrate it (these
        # are small text files, so the download cost is minor).
        meta = parse_csv_meta(path, control)
        if meta is None:
            meta = dict(_EMPTY_CSV)
        h = partial_hash(path)
        entry = (path, meta["rows"], meta["cols"], meta["bbox"],
                 meta["zmin"], meta["zmax"], meta["codes"], meta["terms"], meta["cells"],
                 meta["excluded"], mtime, size)
        if h:
            hash_groups[h].append(entry)
        else:
            no_hash.append((*entry, 1))

    files: list[tuple] = list(no_hash)
    for group in hash_groups.values():
        paths = [e[0] for e in group]
        canon = best_path(paths, priority)
        files.append((*next(e for e in group if e[0] == canon), len(group)))

    rows = []
    for path, nrows, cols, bbox, zmin, zmax, codes, terms, cells, excluded, mtime, size, copies in files:
        rows.append({
            "name":         Path(path).stem,
            "rows":         nrows,
            "cols":         cols,
            "bbox":         bbox,
            "zmin":         zmin,
            "zmax":         zmax,
            "codes":        codes,
            "terms":        terms,
            "cells":        cells,
            "excluded":     excluded,
            "size":         size,
            "copies":       copies,
            "version_note": "",
            "date":         fmt_date(mtime),
            "filename":     Path(path).name,
            "filepath":     path,
            "folder_url":   folder_url(path),
        })
    return rows


def build_scan_rows(results: list[dict], priority: list[str],
                    max_download_bytes: int = 0) -> tuple[list[dict], int]:
    """Index scans from Trimble JobXML (.jxl) files: one row per scan, located by
    parse_jxl_scans. Copies of a job file hash-dedup to the canonical path, and a
    scan that appears in several exports of a job (same name, station and time)
    is listed once. Cloud-only JXLs are read (so OneDrive downloads them) only up
    to max_download_bytes. Returns (rows, number of cloud-only files skipped)."""
    hash_groups: dict[str, list] = defaultdict(list)
    no_hash: list = []
    n_skipped = 0

    for item in results:
        path = str(Path(item["path"]) / item["name"])
        try:
            stat = Path(path).stat()
            mtime, size = stat.st_mtime, stat.st_size
        except OSError:
            continue
        if not is_local(path) and size > max_download_bytes:
            n_skipped += 1
            continue
        scans = parse_jxl_scans(path)
        if not scans:
            continue
        h = partial_hash(path)
        if h:
            hash_groups[h].append((path, scans, mtime, size))
        else:
            no_hash.append((path, scans, mtime, size, 1))

    files: list[tuple] = list(no_hash)
    for group in hash_groups.values():
        canon = best_path([e[0] for e in group], priority)
        files.append((*next(e for e in group if e[0] == canon), len(group)))
    files.sort(key=lambda f: priority_score(f[0], priority))  # canonical copies first

    rows, seen = [], set()
    for path, scans, mtime, size, copies in files:
        for s in scans:
            key = (s["name"], s["station"], s["time"])
            if key in seen:
                continue
            seen.add(key)
            poly = s["poly"]
            bbox = None
            if poly:
                xs, ys = [p[0] for p in poly], [p[1] for p in poly]
                bbox = (min(xs), min(ys), max(xs), max(ys))
            rows.append({
                "name":         s["name"],
                "job":          Path(path).stem,
                "station":      s["station"],
                "station_xy":   s["xy"],
                "pts":          s["points"],
                "raw":          s["raw"],
                "poly":         poly,
                "bbox":         bbox,
                "size":         s["points"],   # --sort size orders scans by point count
                "copies":       copies,
                "version_note": "",
                "date":         s["time"][:10] or fmt_date(mtime),
                "filename":     Path(path).name,
                "filepath":     path,
                "folder_url":   folder_url(path),
            })
    return rows, n_skipped


# ── HTML ──────────────────────────────────────────────────────────────────────

_CSS = """\
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;font-size:14px;
  color:#1a1a2e;background:#eef0f5;min-height:100vh}
header{background:#1a1a2e;color:#fff;padding:1.1rem 2rem;display:flex;align-items:center;gap:1.5rem}
header .hdr-left{flex:1;min-width:0}
header h1{font-size:1.3rem;font-weight:600;letter-spacing:.02em}
header p{margin-top:.3rem;font-size:.78rem;opacity:.6;word-break:break-all}
header img{height:52px;object-fit:contain;flex-shrink:0}
.tabs{background:#16213e;padding:0 2rem;display:flex;gap:.15rem}
.tab-btn{background:transparent;border:none;color:#8892b0;
  padding:.65rem 1.2rem;cursor:pointer;font-size:.88rem;
  border-bottom:3px solid transparent;transition:color .12s}
.tab-btn.active{color:#ccd6f6;border-bottom-color:#64ffda}
.tab-btn:not(.active):hover{color:#ccd6f6}
.panel{padding:1.2rem 2rem}
.toolbar{display:flex;align-items:center;gap:.9rem;margin-bottom:.85rem;flex-wrap:wrap}
.toolbar input{flex:1;max-width:340px;padding:.38rem .7rem;border:1px solid #ccc;
  border-radius:6px;font-size:.88rem;outline:none;background:#fff}
.toolbar input:focus{border-color:#0f3460;box-shadow:0 0 0 2px #0f346028}
.count{color:#666;font-size:.8rem}
table{width:100%;border-collapse:collapse;background:#fff;
  border-radius:8px;overflow:hidden;box-shadow:0 1px 4px #0002}
thead{background:#1a1a2e;color:#ccd6f6;position:sticky;top:0}
th{padding:.58rem 1rem;text-align:left;font-weight:500;cursor:pointer;
  white-space:nowrap;user-select:none;font-size:.82rem}
th:not(:last-child){border-right:1px solid #253047}
th:hover{background:#253047}
th.asc::after{content:" ↑"}
th.desc::after{content:" ↓"}
td{padding:.46rem 1rem;border-bottom:1px solid #eef0f3;
  font-size:.85rem;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:#f4f6ff}
.num{text-align:right;font-variant-numeric:tabular-nums;color:#333}
.opn button{color:#0f3460;border:none;padding:.18rem .5rem;
  background:#e8f0fe;border-radius:4px;font-size:.78rem;white-space:nowrap;cursor:pointer}
.opn button:hover{background:#c7d9f8}
.avail{text-align:center;font-size:.95rem}
.avail.ok{color:#22c55e}
.avail.cl{color:#94a3b8}
.ver{font-size:.7rem;color:#aaa;font-style:italic;margin-left:.35rem}
.fname{color:#444;font-size:.82rem}
.pager{margin-top:.85rem;display:flex;align-items:center;
  gap:.7rem;color:#555;font-size:.82rem;flex-wrap:wrap}
.pager button{padding:.28rem .65rem;border:1px solid #ccc;border-radius:5px;
  background:#fff;cursor:pointer;font-size:.8rem}
.pager button:disabled{opacity:.35;cursor:default}
.pager button:not(:disabled):hover{background:#e8f0fe;border-color:#0f3460}
"""

_JS = """\
const PG=50,T={};
function init(){
  for(const id of['surfaces','alignments','pointclouds','scans','orthophotos','tbcprojects','csvfiles']){
    const d=DATA[id];if(!d||!d.length)continue;
    T[id]={rows:d,fil:d,pg:0,col:null,dir:1};render(id);
  }
  const h=location.hash.slice(1);
  if(h&&document.getElementById('panel-'+h))showTab(h);
}
function filt(id){
  const q=document.getElementById('q-'+id).value.toLowerCase();
  T[id].fil=q?T[id].rows.filter(r=>(r.n+' '+r.f+' '+(r.cd||'')+' '+(r.sx||'')).toLowerCase().includes(q)):T[id].rows;
  T[id].pg=0;render(id);
}
function srt(id,c){const t=T[id];t.dir=t.col===c?-t.dir:1;t.col=c;t.pg=0;render(id);}
function go(id,p){T[id].pg=p;render(id);}
const KEYS={surfaces:['n','p','fa','c','d','f','av'],alignments:['n','len','sta','sta_e','c','d','f','av'],pointclouds:['n','p','den','sz','c','d','f','av'],scans:['n','job','st','p','d','c','f','av'],orthophotos:['n','w','h','gsd','sz','c','d','f','av'],tbcprojects:['n','sz','c','d','f','av'],csvfiles:['n','rows','cols','ncd','sz','c','d','f','av']};
function render(id){
  const t=T[id];let rows=[...t.fil];
  if(t.col!==null){
    const k=KEYS[id][t.col];
    rows.sort((a,b)=>{const av=a[k],bv=b[k];
      return(typeof av==='number'?av-bv:av.localeCompare(bv,undefined,{sensitivity:'base'}))*t.dir;
    });
  }
  const tot=rows.length,pages=Math.max(1,Math.ceil(tot/PG));
  t.pg=Math.min(t.pg,pages-1);
  const sl=rows.slice(t.pg*PG,(t.pg+1)*PG);
  document.getElementById('tb-'+id).innerHTML=sl.map(r=>mkrow(id,r)).join('');
  mkpager(id,t.pg,pages,tot);
  document.querySelectorAll('#ths-'+id+' th').forEach((th,i)=>{
    th.className=t.col===i?(t.dir===1?'asc':'desc'):'';
  });
  document.getElementById('cnt-'+id).textContent=
    tot===t.rows.length?`${tot.toLocaleString()} results`
                       :`${tot.toLocaleString()} of ${t.rows.length.toLocaleString()} results`;
}
function cpy(el,p){navigator.clipboard.writeText(p);el.textContent='Copied!';setTimeout(()=>el.textContent='Copy',1200);}
function fmtsz(b){return b>=1e9?(b/1e9).toFixed(1)+' GB':b>=1e6?(b/1e6).toFixed(1)+' MB':(b/1e3).toFixed(0)+' KB';}
function fmtgsd(m){return m?m>=1?m.toFixed(2)+' m':(m*100).toFixed(1)+' cm':'-';}
function fmtlen(m){return m?m.toLocaleString('en',{maximumFractionDigits:1})+' m':'-';}
function fmtden(d){return d?d.toFixed(1)+' pts/m²':'-';}
function fmtsta(m){if(m===undefined||m===null)return'-';const k=Math.floor(m/1000);return k+'+'+(m%1000).toFixed(3).padStart(7,'0');}
function mkrow(id,r){
  const nm=r.v?`${x(r.n)}<span class="ver">${x(r.v)}</span>`:x(r.n);
  const lk=`<td class="opn"><button data-p="${x(r.fp)}" onclick="cpy(this,this.dataset.p)" title="${x(r.fp)}">Copy</button></td>`;
  const fn=`<td class="fname" title="${x(r.fp)}">${x(r.f)}</td>`;
  const cp=r.c>1?`\xd7${r.c}`:'1';
  const av=r.av?'<td class="avail ok" title="Locally available">✓</td>':'<td class="avail cl" title="Cloud only">☁</td>';
  if(id==='surfaces')
    return`<tr><td>${nm}</td><td class="num">${r.p.toLocaleString()}</td><td class="num">${r.fa.toLocaleString()}</td><td class="num">${cp}</td><td>${r.d}</td>${fn}${av}${lk}</tr>`;
  if(id==='pointclouds')
    return`<tr><td>${nm}</td><td class="num">${r.p.toLocaleString()}</td><td class="num">${fmtden(r.den)}</td><td class="num">${fmtsz(r.sz)}</td><td class="num">${cp}</td><td>${r.d}</td>${fn}${av}${lk}</tr>`;
  if(id==='orthophotos'){
    const gsdCell=`<td class="num" title="${x(r.bb||'')}">${fmtgsd(r.gsd)}</td>`;
    return`<tr><td>${nm}</td><td class="num">${r.w?r.w.toLocaleString():'-'}</td><td class="num">${r.h?r.h.toLocaleString():'-'}</td>${gsdCell}<td class="num">${fmtsz(r.sz)}</td><td class="num">${cp}</td><td>${r.d}</td>${fn}${av}${lk}</tr>`;}
  if(id==='alignments')
    return`<tr><td>${nm}</td><td class="num">${fmtlen(r.len)}</td><td class="num">${fmtsta(r.sta)}</td><td class="num">${fmtsta(r.sta_e)}</td><td class="num">${cp}</td><td>${r.d}</td>${fn}${av}${lk}</tr>`;
  if(id==='scans'){
    const stCell=`<td title="${x(r.sxy||'')}">${x(r.st)}</td>`;
    return`<tr><td title="${x(r.raw||'')}">${nm}</td><td>${x(r.job)}</td>${stCell}<td class="num">${r.p.toLocaleString()}</td><td>${r.d}</td><td class="num">${cp}</td>${fn}${av}${lk}</tr>`;}
  if(id==='tbcprojects')
    return`<tr><td>${nm}</td><td class="num">${fmtsz(r.sz)}</td><td class="num">${cp}</td><td>${r.d}</td>${fn}${av}${lk}</tr>`;
  if(id==='csvfiles'){
    const ptsCell=`<td class="num" title="${x(r.bb||'')}">${r.rows.toLocaleString()}</td>`;
    const cdCell=`<td class="num" title="${x(r.cd||'')}">${r.ncd?r.ncd.toLocaleString():'-'}</td>`;
    return`<tr><td>${nm}</td>${ptsCell}<td class="num">${r.cols?r.cols.toLocaleString():'-'}</td>${cdCell}<td class="num">${fmtsz(r.sz)}</td><td class="num">${cp}</td><td>${r.d}</td>${fn}${av}${lk}</tr>`;}
  return`<tr><td>${nm}</td><td class="num">${cp}</td><td>${r.d}</td>${fn}${av}${lk}</tr>`;
}
function mkpager(id,pg,pages,tot){
  const el=document.getElementById('pg-'+id);
  if(pages<=1){el.textContent='';return;}
  const s=pg*PG+1,en=Math.min((pg+1)*PG,tot);
  el.innerHTML=
    `<button onclick="go('${id}',${pg-1})"${pg===0?' disabled':''}>&#8592; Prev</button>`+
    `<span>Page ${pg+1} of ${pages} · ${s}–${en} of ${tot.toLocaleString()}</span>`+
    `<button onclick="go('${id}',${pg+1})"${pg===pages-1?' disabled':''}>Next &#8594;</button>`;
}
function showTab(id){
  document.querySelectorAll('.panel').forEach(p=>p.hidden=true);
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('panel-'+id).hidden=false;
  document.getElementById('btn-'+id).classList.add('active');
  history.replaceState(null,'','#'+id);  // so a link or refresh reopens this tab
  if(id==='map')mInit();                 // the canvas needs a visible panel to size itself
}
function x(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
window.addEventListener('load',init);
"""


# ── map tab ───────────────────────────────────────────────────────────────────
# A plain <canvas> map in project coordinates: no basemap and no libraries, so the
# report stays one self-contained file that works offline. Data comes from MAP
# (see build_map_data).

_MAP_CSS = """\
.m-bar{display:flex;align-items:center;gap:.55rem .9rem;margin-bottom:.6rem;flex-wrap:wrap;font-size:.8rem;color:#333}
.lay{display:inline-flex;align-items:center;gap:.3rem;cursor:pointer;user-select:none}
.lay i,.m-grp i{display:inline-block;width:10px;height:10px;border-radius:2px}
.mbtn{padding:.3rem .7rem;border:1px solid #ccc;border-radius:5px;background:#fff;cursor:pointer;font-size:.8rem}
.mbtn.on{background:#0f3460;color:#fff;border-color:#0f3460}
.mbtn:not(.on):hover{background:#e8f0fe}
.m-bar input[type=number]{width:4.5em;padding:.2rem .3rem;border:1px solid #ccc;border-radius:4px;font-size:.8rem}
.m-hint{color:#666}
.map-wrap{display:flex;gap:1rem}
.map-box{position:relative;flex:1;min-width:0;height:calc(100vh - 235px);min-height:420px;background:#f8fafc;
  border-radius:8px;box-shadow:0 1px 4px #0002;overflow:hidden}
.map-box canvas{display:block;width:100%;height:100%;cursor:crosshair}
.map-box.drop{outline:3px dashed #0f3460;outline-offset:-3px}
.m-xy{position:absolute;left:8px;bottom:6px;font-size:.74rem;color:#475569;background:#fffc;padding:1px 6px;border-radius:4px;
  font-variant-numeric:tabular-nums;pointer-events:none}
.map-res{width:390px;flex-shrink:0;height:calc(100vh - 235px);min-height:420px;overflow:auto;background:#fff;
  border-radius:8px;box-shadow:0 1px 4px #0002;padding:.7rem .85rem;font-size:.82rem}
.m-empty{color:#666;line-height:1.5}
.m-head{color:#333;margin-bottom:.5rem;line-height:1.45}
.m-grp{display:flex;align-items:center;gap:.4rem;font-weight:600;margin:.8rem 0 .3rem;color:#1a1a2e}
.mr{padding:.35rem .4rem;border-radius:5px;border-bottom:1px solid #eef0f3}
.mr:hover{background:#f4f6ff}
.mr-n{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mr-m{color:#666;font-size:.76rem;margin:.1rem 0 .2rem}
.mr-b button{color:#0f3460;border:none;padding:.12rem .45rem;background:#e8f0fe;border-radius:4px;font-size:.74rem;
  cursor:pointer;margin-right:.3rem}
.mr-b button:hover{background:#c7d9f8}
@media(max-width:900px){.map-wrap{flex-direction:column}.map-res{width:auto;height:auto;min-height:0}}
"""

_MAP_PANEL = """\
<div id="panel-map" class="panel" hidden>
  <div class="m-bar" id="m-layers"></div>
  <div class="m-bar">
    <button id="m-pick" class="mbtn on" onclick="mMode('pick')">Pick point</button>
    <label>within <input id="m-rad" type="number" value="10" min="0.5" step="1" onchange="mRadius()"> m</label>
    <button id="m-draw" class="mbtn" onclick="mMode('draw')">Draw area</button>
    <button class="mbtn" onclick="document.getElementById('m-file').click()">Load DXF boundary…</button>
    <input id="m-file" type="file" accept=".dxf" hidden onchange="mLoadFile(this.files[0]);this.value=''">
    <button class="mbtn" onclick="mClear()">Clear</button>
    <button class="mbtn" onclick="mFitDefault();mDraw()">Reset view</button>
    <span class="m-hint" id="m-hint">Click the map to list files with data there · drag to pan, scroll to zoom · drop a DXF boundary onto the map</span>
  </div>
  <div class="map-wrap">
    <div class="map-box" id="m-box"><canvas id="m-cv"></canvas><div class="m-xy" id="m-xy"></div></div>
    <div class="map-res" id="m-res"></div>
  </div>
</div>
"""

_MAP_JS = """\
// Layer key → [label, colour]. Coordinates in MAP are metres relative to MAP.o.
const MT={c:['CSV points','#16a34a'],n:['Scans','#c026d3'],s:['Surfaces','#2563eb'],p:['Point clouds','#ea580c'],
  o:['Orthophotos','#a16207'],a:['Alignments','#475569'],k:['Control','#dc2626']};
const M={on:{c:1,n:1,s:1,p:1,o:0,a:1,k:1,b:1},mode:'pick',sel:null,draft:[],hl:-1,res:[],ready:false,drag:null,v:null,tiles:{}};
if(MAP.bm)MT.b=['Background','#94a3b8'];
function pairs(a){const o=[];for(let i=0;i+1<a.length;i+=2)o.push([a[i],a[i+1]]);return o;}
function mPrep(){
  for(const f of MAP.f){
    const b=[Infinity,Infinity,-Infinity,-Infinity];
    const ext=(e,n)=>{b[0]=Math.min(b[0],e);b[1]=Math.min(b[1],n);b[2]=Math.max(b[2],e);b[3]=Math.max(b[3],n);};
    if(f.c){  // decode delta cells → flat [col,row,count,…]
      const sz=MAP.cell*f.k,cs=[];let i=0,j=0,q=0;
      for(let t=0;t<f.c.length;t+=3){const dj=f.c[t];j+=dj;i=dj?f.c[t+1]:i+f.c[t+1];cs.push(i,j,f.c[t+2]);q+=f.c[t+2];
        ext(i*sz,j*sz);ext((i+1)*sz,(j+1)*sz);}
      f.cs=cs;f.sz=sz;f.q=q;
    }else if(f.g){f.poly=pairs(f.g);f.poly.forEach(p=>ext(p[0],p[1]));f.stp=f.st;}
    else if(f.l){f.line=pairs(f.l);f.line.forEach(p=>ext(p[0],p[1]));}
    else if(f.b){const[a,c,d,e]=f.b;f.poly=[[a,c],[d,c],[d,e],[a,e]];ext(a,c);ext(d,e);f.area=(d-a)*(e-c);}
    f.bb=b;
  }
  M.ctrl=pairs(MAP.k);
  // Coverage per grid layer: each cell once, with how many files have points in
  // it. Drawing files one by one would stack hundreds of site-wide exports into a
  // solid blanket; this shades by how much data there is instead.
  M.cov={};
  for(const t of['s','c']){
    const m=new Map();
    for(const f of MAP.f)if(f.t===t&&f.cs)
      for(let i=0;i<f.cs.length;i+=3){const k=f.sz+':'+f.cs[i]+':'+f.cs[i+1];m.set(k,(m.get(k)||0)+1);}
    M.cov[t]=[...m].map(([k,c])=>{const[s,i,j]=k.split(':').map(Number);return[i*s,j*s,s,c];});
  }
}
function mInit(){
  if(!M.ready){
    M.ready=true;mPrep();
    document.getElementById('m-layers').innerHTML='Show: '+Object.keys(MT)
      .filter(t=>t==='k'?M.ctrl.length:t==='b'?MAP.bm:MAP.f.some(f=>f.t===t))
      .map(t=>`<label class="lay"${t==='b'?` title="${x(MAP.bm.name)}"`:''}><input type="checkbox"${M.on[t]?' checked':''} onchange="M.on['${t}']=this.checked;mQuery()"><i style="background:${MT[t][1]}"></i>${MT[t][0]}</label>`).join('');
    M.cv=document.getElementById('m-cv');M.g=M.cv.getContext('2d');M.box=document.getElementById('m-box');
    mEvents();mRes();
  }
  mSize();if(!M.v)mFitDefault();mDraw();
}
function mSize(){const r=M.box.getBoundingClientRect();M.dpr=window.devicePixelRatio||1;M.w=r.width;M.h=r.height;
  M.cv.width=Math.round(r.width*M.dpr);M.cv.height=Math.round(r.height*M.dpr);}
function mFit(b){const pad=1.15;M.v={cx:(b[0]+b[2])/2,cy:(b[1]+b[3])/2,
  s:Math.min(M.w/Math.max(b[2]-b[0],10),M.h/Math.max(b[3]-b[1],10))/pad};}
function mFitDefault(){
  // Fit the main site: files within 5 km of the median file position (so files in
  // another coordinate system don't zoom the view out), 2nd–98th percentile extent.
  const fs=MAP.f.filter(f=>'csn'.includes(f.t)&&isFinite(f.bb[0]));
  if(!fs.length){M.v={cx:0,cy:0,s:1};return;}
  const ce=fs.map(f=>(f.bb[0]+f.bb[2])/2),cn=fs.map(f=>(f.bb[1]+f.bb[3])/2);
  const med=a=>a.slice().sort((p,q)=>p-q)[a.length>>1],me=med(ce),mn=med(cn);
  const es=[],ns=[];
  fs.forEach((f,i)=>{if(Math.hypot(ce[i]-me,cn[i]-mn)<5000){es.push(f.bb[0],f.bb[2]);ns.push(f.bb[1],f.bb[3]);}});
  es.sort((a,b)=>a-b);ns.sort((a,b)=>a-b);const q=(a,p)=>a[Math.floor(p*(a.length-1))];
  mFit([q(es,.02),q(ns,.02),q(es,.98),q(ns,.98)]);
}
function mAt(e){const r=M.cv.getBoundingClientRect();
  return[M.v.cx+(e.clientX-r.left-M.w/2)/M.v.s,M.v.cy-(e.clientY-r.top-M.h/2)/M.v.s];}
function mDraw(){
  if(!M.ready||!M.v)return;
  const g=M.g,v=M.v,w=M.w,h=M.h;
  g.setTransform(M.dpr,0,0,M.dpr,0,0);g.globalAlpha=1;g.fillStyle='#f8fafc';g.fillRect(0,0,w,h);
  const X=e=>(e-v.cx)*v.s+w/2,Y=n=>h/2-(n-v.cy)*v.s;
  const vb=[v.cx-w/2/v.s,v.cy-h/2/v.s,v.cx+w/2/v.s,v.cy+h/2/v.s];
  const vis=f=>M.on[f.t]&&f.bb[2]>=vb[0]&&f.bb[0]<=vb[2]&&f.bb[3]>=vb[1]&&f.bb[1]<=vb[3];
  const path=(pts,close)=>{g.beginPath();pts.forEach((p,i)=>i?g.lineTo(X(p[0]),Y(p[1])):g.moveTo(X(p[0]),Y(p[1])));if(close)g.closePath();};
  if(MAP.bm&&M.on.b)mBase(X,Y,vb);
  const cells=(f,a)=>{g.globalAlpha=a;g.fillStyle=MT[f.t][1];const s=Math.max(f.sz*v.s,1);
    for(let t=0;t<f.cs.length;t+=3)g.fillRect(X(f.cs[t]*f.sz),Y((f.cs[t+1]+1)*f.sz),s,s);};
  g.lineWidth=1;g.setLineDash([4,3]);  // header extents: outline only
  for(const f of MAP.f)if((f.t==='p'||f.t==='o')&&vis(f)){g.globalAlpha=.7;g.strokeStyle=MT[f.t][1];path(f.poly,true);g.stroke();}
  g.setLineDash([]);
  for(const t of['s','c']){  // coverage: more files with points here → darker
    if(!M.on[t])continue;const base=(t==='s'?.4:.6)*(MAP.bm&&M.on.b?.6:1);g.fillStyle=MT[t][1];
    for(const[e,n,s,c]of M.cov[t]){if(e+s<vb[0]||e>vb[2]||n+s<vb[1]||n>vb[3])continue;
      g.globalAlpha=base*(1-Math.pow(.7,c));const px=Math.max(s*v.s,1);g.fillRect(X(e),Y(n+s),px,px);}}
  for(const f of MAP.f)if(f.t==='n'&&vis(f)){
    g.globalAlpha=.1;g.fillStyle=MT.n[1];path(f.poly,true);g.fill();g.globalAlpha=.6;g.strokeStyle=MT.n[1];g.stroke();
    g.globalAlpha=1;g.beginPath();g.arc(X(f.stp[0]),Y(f.stp[1]),3,0,7);g.fill();}
  g.globalAlpha=.75;g.strokeStyle=MT.a[1];g.lineWidth=1.3;
  for(const f of MAP.f)if(f.t==='a'&&vis(f)){path(f.line,false);g.stroke();}
  if(M.on.k){g.globalAlpha=1;g.fillStyle=MT.k[1];
    for(const[e,n]of M.ctrl){const px=X(e),py=Y(n);if(px<-6||px>w+6||py<-6||py>h+6)continue;
      g.beginPath();g.moveTo(px,py-5);g.lineTo(px+4.5,py+3);g.lineTo(px-4.5,py+3);g.closePath();g.fill();}}
  const hf=M.hl>=0&&M.res[M.hl]?M.res[M.hl].f:null;  // result under the mouse
  if(hf){g.lineWidth=2.5;g.strokeStyle=MT[hf.t][1];if(hf.cs)cells(hf,.7);else{g.globalAlpha=1;path(hf.poly,true);g.stroke();}}
  g.globalAlpha=1;g.strokeStyle='#f97316';g.fillStyle='#f97316';g.lineWidth=2;
  const sel=M.sel;
  if(sel&&sel.type==='pt'){g.beginPath();g.arc(X(sel.e),Y(sel.n),Math.max(sel.r*v.s,3),0,7);
    g.globalAlpha=.12;g.fill();g.globalAlpha=1;g.stroke();}
  if(sel&&sel.type==='poly')for(const P of sel.polys){path(P,true);g.globalAlpha=.08;g.fill();g.globalAlpha=1;g.stroke();}
  if(M.draft.length){path(M.draft,false);g.stroke();for(const p of M.draft){g.beginPath();g.arc(X(p[0]),Y(p[1]),3,0,7);g.fill();}}
  mScale();
}
function mBase(X,Y,vb){
  // Orthophoto tiles (see build_basemap): the coarsest level is always drawn
  // first so there's a picture while the detailed tiles for this zoom load.
  const B=MAP.bm,g=M.g,v=M.v,top=B.levels.length-1;
  const want=Math.floor(Math.log2(1/(v.s*M.dpr)/B.gsd));  // finest level not sharper than the screen
  const L=Math.max(0,Math.min(top,want));
  g.globalAlpha=1;
  for(const lv of L===top?[top]:[top,L]){
    const[wL,hL]=B.levels[lv],gs=B.gsd*B.levels[0][0]/wL,ts=B.tile*gs;
    const tx0=Math.max(0,Math.floor((vb[0]-B.x0)/ts)),tx1=Math.min(Math.ceil(wL/B.tile)-1,Math.floor((vb[2]-B.x0)/ts));
    const ty0=Math.max(0,Math.floor((B.y0-vb[3])/ts)),ty1=Math.min(Math.ceil(hL/B.tile)-1,Math.floor((B.y0-vb[1])/ts));
    for(let ty=ty0;ty<=ty1;ty++)for(let tx=tx0;tx<=tx1;tx++){
      const im=mTile(lv,tx,ty);if(!im.complete||!im.naturalWidth)continue;
      g.drawImage(im,X(B.x0+tx*ts),Y(B.y0-ty*ts),im.naturalWidth*gs*v.s+.5,im.naturalHeight*gs*v.s+.5);
    }
  }
}
function mTile(l,tx,ty){
  const k=l+'_'+tx+'_'+ty;let im=M.tiles[k];
  if(!im){
    im=new Image();
    im.onload=()=>{if(!M.raf)M.raf=requestAnimationFrame(()=>{M.raf=0;mDraw();});};
    im.onerror=()=>{document.getElementById('m-hint').textContent=
      `Background tiles not found: the "${MAP.bm.dir}" folder must sit next to this report.`;};
    im.src=encodeURIComponent(MAP.bm.dir)+'/'+k+'.jpg';M.tiles[k]=im;
  }
  return im;
}
function mScale(){
  const g=M.g,s=M.v.s;let L=Math.pow(10,Math.floor(Math.log10(140/s)));
  for(const m of[5,2,1])if(L*m*s<=140){L*=m;break;}
  const px=L*s,x0=M.w-px-16,y0=M.h-16;
  g.globalAlpha=1;g.fillStyle='#ffffffd0';g.fillRect(x0-8,y0-22,px+16,28);  // readable over imagery
  g.strokeStyle='#334155';g.lineWidth=2;g.beginPath();
  g.moveTo(x0,y0-5);g.lineTo(x0,y0);g.lineTo(x0+px,y0);g.lineTo(x0+px,y0-5);g.stroke();
  g.fillStyle='#334155';g.font='11px system-ui,sans-serif';g.textAlign='center';
  g.fillText(L>=1000?L/1000+' km':L+' m',x0+px/2,y0-7);g.textAlign='start';
}
function mEvents(){
  const cv=M.cv,box=M.box;
  cv.addEventListener('mousedown',e=>{M.drag={x:e.clientX,y:e.clientY,cx:M.v.cx,cy:M.v.cy,moved:false};});
  window.addEventListener('mousemove',e=>{
    const d=M.drag;
    if(d){const dx=e.clientX-d.x,dy=e.clientY-d.y;if(Math.abs(dx)+Math.abs(dy)>3)d.moved=true;
      if(d.moved){M.v.cx=d.cx-dx/M.v.s;M.v.cy=d.cy+dy/M.v.s;mDraw();}}
    if(e.target===cv){const[E,N]=mAt(e);
      document.getElementById('m-xy').textContent=`E ${fmtc(E+MAP.o[0])}   N ${fmtc(N+MAP.o[1])}`;}
  });
  window.addEventListener('mouseup',e=>{const d=M.drag;M.drag=null;if(d&&!d.moved&&e.target===cv)mClick(e);});
  cv.addEventListener('dblclick',e=>{e.preventDefault();if(M.mode==='draw')mClose();});
  cv.addEventListener('wheel',e=>{e.preventDefault();const[E,N]=mAt(e),r=cv.getBoundingClientRect();
    M.v.s=Math.min(Math.max(M.v.s*Math.pow(1.0015,-e.deltaY),1e-4),200);
    M.v.cx=E-(e.clientX-r.left-M.w/2)/M.v.s;M.v.cy=N+(e.clientY-r.top-M.h/2)/M.v.s;mDraw();},{passive:false});
  window.addEventListener('resize',()=>{if(!document.getElementById('panel-map').hidden){mSize();mDraw();}});
  window.addEventListener('keydown',e=>{if(e.key==='Escape'&&M.draft.length){M.draft=[];mDraw();}});
  box.addEventListener('dragover',e=>{e.preventDefault();box.classList.add('drop');});
  box.addEventListener('dragleave',()=>box.classList.remove('drop'));
  box.addEventListener('drop',e=>{e.preventDefault();box.classList.remove('drop');mLoadFile(e.dataTransfer.files[0]);});
}
function mClick(e){
  const[E,N]=mAt(e);
  if(M.mode==='pick'){M.sel={type:'pt',e:E,n:N,r:mR()};mQuery();return;}
  const d=M.draft,px=p=>Math.hypot((p[0]-E)*M.v.s,(p[1]-N)*M.v.s);
  if(d.length&&px(d[d.length-1])<4)return;       // second click of a double-click
  if(d.length>=3&&px(d[0])<9){mClose();return;}   // clicked the first corner
  d.push([E,N]);mDraw();
}
function mR(){return Math.max(parseFloat(document.getElementById('m-rad').value)||0,0.5);}
function mRadius(){if(M.sel&&M.sel.type==='pt'){M.sel.r=mR();mQuery();}}
function mClose(){
  if(M.draft.length>=3)M.sel={type:'poly',polys:[M.draft],label:`drawn area (${fmtarea(polyArea(M.draft))})`};
  M.draft=[];mQuery();
}
function mMode(m){
  M.mode=m;M.draft=[];
  document.getElementById('m-pick').classList.toggle('on',m==='pick');
  document.getElementById('m-draw').classList.toggle('on',m==='draw');
  document.getElementById('m-hint').textContent=m==='pick'
    ?'Click the map to list files with data there · drag to pan, scroll to zoom · drop a DXF boundary onto the map'
    :'Click to add corners · click the first corner or double-click to finish · Esc cancels';
  mDraw();
}
function mClear(){M.sel=null;M.draft=[];M.res=[];M.hl=-1;mRes();mDraw();}
// ── geometry (plan metres) ──
function pip(x,y,P){let h=false;for(let i=0,j=P.length-1;i<P.length;j=i++){const[xi,yi]=P[i],[xj,yj]=P[j];
  if((yi>y)!==(yj>y)&&x<(xj-xi)*(y-yi)/(yj-yi)+xi)h=!h;}return h;}
function segX(a,b,c,d){const o=(p,q,r)=>Math.sign((q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0]));
  return o(a,b,c)!==o(a,b,d)&&o(c,d,a)!==o(c,d,b);}
function polysMeet(A,B){
  if(A.some(p=>pip(p[0],p[1],B))||B.some(p=>pip(p[0],p[1],A)))return true;
  for(let i=0;i<A.length;i++)for(let j=0;j<B.length;j++)
    if(segX(A[i],A[(i+1)%A.length],B[j],B[(j+1)%B.length]))return true;
  return false;}
function segDist(p,a,b){const dx=b[0]-a[0],dy=b[1]-a[1],L=dx*dx+dy*dy;
  const t=L?Math.max(0,Math.min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/L)):0;
  return Math.hypot(p[0]-a[0]-t*dx,p[1]-a[1]-t*dy);}
function polyNear(P,e,n,r){if(pip(e,n,P))return true;
  for(let i=0;i<P.length;i++)if(segDist([e,n],P[i],P[(i+1)%P.length])<=r)return true;return false;}
function polyArea(P){let a=0;for(let i=0,j=P.length-1;i<P.length;j=i++)a+=(P[j][0]+P[i][0])*(P[j][1]-P[i][1]);return Math.abs(a/2);}
// ── search: which files have data in the selection ──
const ORDER='cnsop';  // CSV points, scans, surfaces, point clouds, orthophotos
function mQuery(){
  const sel=M.sel;M.res=[];M.hl=-1;
  if(sel){
    let sb;
    if(sel.type==='pt')sb=[sel.e-sel.r,sel.n-sel.r,sel.e+sel.r,sel.n+sel.r];
    else{sb=[Infinity,Infinity,-Infinity,-Infinity];
      for(const P of sel.polys)for(const[e,n]of P){sb[0]=Math.min(sb[0],e);sb[1]=Math.min(sb[1],n);sb[2]=Math.max(sb[2],e);sb[3]=Math.max(sb[3],n);}}
    for(const f of MAP.f){
      if(f.t==='a'||!M.on[f.t])continue;
      const b=f.bb;if(b[2]<sb[0]||b[0]>sb[2]||b[3]<sb[1]||b[1]>sb[3])continue;
      if(f.cs){  // grid footprint: count the points in cells the selection touches
        let q=0;const s=f.sz;
        for(let t=0;t<f.cs.length;t+=3){
          const x0=f.cs[t]*s,y0=f.cs[t+1]*s,x1=x0+s,y1=y0+s;
          if(x1<sb[0]||x0>sb[2]||y1<sb[1]||y0>sb[3])continue;
          let hit;
          if(sel.type==='pt'){const dx=Math.max(x0-sel.e,0,sel.e-x1),dy=Math.max(y0-sel.n,0,sel.n-y1);hit=dx*dx+dy*dy<=sel.r*sel.r;}
          else hit=sel.polys.some(P=>pip(x0+s/2,y0+s/2,P)||P.some(([e,n])=>e>=x0&&e<=x1&&n>=y0&&n<=y1));
          if(hit)q+=f.cs[t+2];
        }
        if(q)M.res.push({f,q});
      }else{
        const hit=sel.type==='pt'?polyNear(f.poly,sel.e,sel.n,sel.r):sel.polys.some(P=>polysMeet(f.poly,P));
        if(hit)M.res.push({f,q:null});
      }
    }
    // Most points inside first; scans by size; extent-only files smallest first
    // (a box around the whole site says little about one building).
    M.res.sort((a,b)=>ORDER.indexOf(a.f.t)-ORDER.indexOf(b.f.t)
      ||(a.q!==null&&b.q!==null?b.q-a.q:0)||(a.f.t==='n'?b.f.q-a.f.q:0)||((a.f.area||0)-(b.f.area||0)));
  }
  mRes();mDraw();
}
function mRes(){
  const el=document.getElementById('m-res'),sel=M.sel;
  if(!sel){el.innerHTML='<p class="m-empty">Click the map to list the files with data at that spot, '+
    'use <b>Draw area</b> to outline a building, or drop a DXF boundary (polylines) onto the map.<br><br>'+
    'CSV points and surfaces are ranked by how many of their points fall inside. Scans are placed from '+
    'their instrument station and scan frame (approximate). Point clouds and orthophotos only have their extent.</p>';return;}
  const head=sel.type==='pt'?`Within ${sel.r} m of E ${fmtc(sel.e+MAP.o[0])}, N ${fmtc(sel.n+MAP.o[1])}`:`Inside ${x(sel.label)}`;
  const groups={};M.res.forEach((r,i)=>(groups[r.f.t]=groups[r.f.t]||[]).push(i));
  let h=`<div class="m-head">${head}<br><b>${M.res.length.toLocaleString()}</b> file${M.res.length===1?'':'s'}</div>`;
  for(const t of ORDER){
    const g=groups[t];if(!g)continue;
    h+=`<div class="m-grp"><i style="background:${MT[t][1]}"></i>${MT[t][0]} (${g.length})</div>`;
    for(const i of g.slice(0,300)){
      const r=M.res[i],f=r.f;let m;
      // ≈: big footprints are drawn with merged cells, so their count is approximate
      if(r.q!==null)m=`${f.k>1?'≈ ':''}${r.q.toLocaleString()} of ${f.q.toLocaleString()} pts inside${f.x?` · ${f.x} control left out`:''}`;
      else if(f.t==='n')m=`${f.q.toLocaleString()} pts · scan aimed here`;
      else m=`extent overlaps · ${fmtarea(f.area)}`;
      h+=`<div class="mr" onmouseenter="mHl(${i})" onmouseleave="mHl(-1)"><div class="mr-n" title="${x(f.p)}">${x(f.n)}</div>`+
         `<div class="mr-m">${f.d} · ${m}</div><div class="mr-b"><button data-p="${x(f.p)}" onclick="cpy(this,this.dataset.p)">Copy path</button>`+
         `<button onclick="mZoom(${i})">Zoom to</button></div></div>`;
    }
    if(g.length>300)h+=`<div class="mr-m">… and ${g.length-300} more</div>`;
  }
  el.innerHTML=h;
}
function mHl(i){M.hl=i;mDraw();}
function mZoom(i){mFit(M.res[i].f.bb);mDraw();}
function mLoadFile(file){
  if(!file)return;
  const rd=new FileReader();
  rd.onload=()=>{
    const polys=mDxf(String(rd.result));
    if(!polys.length){document.getElementById('m-hint').textContent=
      `No polylines found in ${file.name}. Save the boundary as an ASCII DXF with POLYLINE or LWPOLYLINE entities.`;return;}
    M.sel={type:'poly',polys,label:`${file.name} (${polys.length} polyline${polys.length>1?'s':''})`};
    const b=[Infinity,Infinity,-Infinity,-Infinity];
    for(const P of polys)for(const[e,n]of P){b[0]=Math.min(b[0],e);b[1]=Math.min(b[1],n);b[2]=Math.max(b[2],e);b[3]=Math.max(b[3],n);}
    const dx=(b[2]-b[0])*.6,dy=(b[3]-b[1])*.6;mFit([b[0]-dx,b[1]-dy,b[2]+dx,b[3]+dy]);mQuery();
  };
  rd.readAsText(file);
}
function mDxf(text){
  // Polylines in the ENTITIES section of an ASCII DXF: LWPOLYLINE (vertices as
  // repeated 10/20 codes) and POLYLINE (a VERTEX entity per vertex, then SEQEND).
  const L=text.split(/\\r?\\n/),polys=[];let ent=null,cur=null,xv=null,inEnt=false;
  const done=()=>{if(cur&&cur.length>2)polys.push(cur);cur=null;};
  for(let i=0;i+1<L.length;i+=2){
    const c=L[i].trim(),v=L[i+1].trim();
    if(c==='2'&&v==='ENTITIES')inEnt=true;
    if(c==='0'){
      if(v==='ENDSEC'){done();inEnt=false;ent=null;}
      else if(v==='LWPOLYLINE'){done();cur=[];ent='LW';}
      else if(v==='POLYLINE'){done();cur=[];ent='PL';}
      else if(v==='VERTEX')ent=(ent==='PL'||ent==='V')?'V':null;
      else{if(ent==='LW'||v==='SEQEND')done();ent=null;}
      continue;
    }
    if(!inEnt||!(ent==='LW'||ent==='V'))continue;
    if(c==='10')xv=parseFloat(v);
    else if(c==='20'&&xv!==null){cur.push([xv-MAP.o[0],parseFloat(v)-MAP.o[1]]);xv=null;}
  }
  done();return polys;
}
function fmtc(v){return v.toLocaleString('en',{minimumFractionDigits:1,maximumFractionDigits:1});}
function fmtarea(a){return a>=1e4?(a/1e4).toLocaleString('en',{maximumFractionDigits:1})+' ha':Math.round(a).toLocaleString()+' m²';}
"""


def _panel(tab_id: str, col_headers: list, first: bool) -> str:
    def _th(i, h):
        label, tip = (h if isinstance(h, tuple) else (h, ""))
        t = f' title="{tip}"' if tip else ""
        return f'<th onclick="srt(\'{tab_id}\',{i})"{t}>{label}</th>'
    ths = "".join(_th(i, h) for i, h in enumerate(col_headers))
    label = tab_id.capitalize()
    hidden = "" if first else " hidden"
    return (
        f'<div id="panel-{tab_id}" class="panel"{hidden}>\n'
        f'  <div class="toolbar">\n'
        f'    <input id="q-{tab_id}" type="search" placeholder="Filter {label.lower()}…"'
        f' oninput="filt(\'{tab_id}\')">\n'
        f'    <span class="count" id="cnt-{tab_id}"></span>\n'
        f'  </div>\n'
        f'  <table>\n'
        f'    <thead><tr id="ths-{tab_id}">{ths}</tr></thead>\n'
        f'    <tbody id="tb-{tab_id}"></tbody>\n'
        f'  </table>\n'
        f'  <div class="pager" id="pg-{tab_id}"></div>\n'
        f'</div>\n'
    )


def render_html(
    rows_by_tag: dict[str, list[dict]],
    search_path: str,
    generated_at: str,
    title: str = "Survey File Index",
    logo_path: Path | None = None,
    map_data: dict | None = None,
) -> str:
    surfaces     = rows_by_tag.get("Surface", [])
    alignments   = rows_by_tag.get("Alignment", [])
    scans        = rows_by_tag.get("Scan", [])
    pointclouds  = rows_by_tag.get("PointCloud", [])
    orthophotos  = rows_by_tag.get("Ortho", [])
    tbcprojects  = rows_by_tag.get("TBC", [])
    csvfiles     = rows_by_tag.get("CSV", [])

    def av(r): return int(is_local(r["filepath"]))
    def ser_surface(r):
        return {"n": r["name"], "p": r["pts"], "fa": r["faces"], "c": r["copies"],
                "d": r["date"], "f": r["filename"], "fp": r["filepath"], "v": r["version_note"], "av": av(r)}
    def ser_alignment(r):
        sta_s = r.get("sta_start", 0.0)
        sta_e = sta_s + r["length"]
        return {"n": r["name"], "len": round(r["length"], 3),
                "sta": round(sta_s, 3), "sta_e": round(sta_e, 3),
                "c": r["copies"], "d": r["date"],
                "f": r["filename"], "fp": r["filepath"], "v": r["version_note"], "av": av(r)}
    def ser_pointcloud(r):
        return {"n": r["name"], "p": r["pts"], "den": round(r["density"], 2), "sz": r["size"], "c": r["copies"],
                "d": r["date"], "f": r["filename"], "fp": r["filepath"], "v": r["version_note"], "av": av(r)}
    def ser_ortho(r):
        bb = r["bbox"]
        bb_str = f"X: {bb[0]:,.0f}–{bb[2]:,.0f}  Y: {bb[1]:,.0f}–{bb[3]:,.0f}" if bb else ""
        return {"n": r["name"], "w": r["width"], "h": r["height"],
                "gsd": round(r["gsd"], 6), "bb": bb_str, "sz": r["size"],
                "c": r["copies"], "d": r["date"], "f": r["filename"],
                "fp": r["filepath"], "v": r["version_note"], "av": av(r)}
    def ser_scan(r):
        xy = r.get("station_xy")
        sxy = f"Station E {xy[0]:,.2f}  N {xy[1]:,.2f}" if xy else "Station not located"
        # sx: job and station are searchable from the filter box too
        return {"n": r["name"], "job": r["job"], "st": r["station"], "sxy": sxy,
                "p": r["pts"], "raw": r["raw"], "sx": f"{r['job']} {r['station']}",
                "c": r["copies"], "d": r["date"], "f": r["filename"], "fp": r["filepath"],
                "v": r["version_note"], "av": av(r)}
    def ser_vce(r):
        return {"n": r["name"], "sz": r["size"], "c": r["copies"],
                "d": r["date"], "f": r["filename"], "fp": r["filepath"], "v": r["version_note"], "av": av(r)}
    def ser_csv(r):
        bb = r["bbox"]
        zmin, zmax = r.get("zmin"), r.get("zmax")
        parts = []
        if bb:
            parts.append(f"E: {bb[0]:,.1f}–{bb[2]:,.1f}")
            parts.append(f"N: {bb[1]:,.1f}–{bb[3]:,.1f}")
        if zmin is not None and zmax is not None:
            parts.append(f"Z: {zmin:,.1f}–{zmax:,.1f}")
        codes = r.get("codes") or []
        # sx: extra searchable text (all low-cardinality columns) beyond the codes
        # already carried in cd — keeps the filter matching file content, not just names.
        extra = [t for t in (r.get("terms") or []) if t not in set(codes)]
        return {"n": r["name"], "rows": r["rows"], "cols": r["cols"], "bb": "  ".join(parts),
                "ncd": len(codes), "cd": ", ".join(codes), "sx": " ".join(extra),
                "sz": r["size"], "c": r["copies"], "d": r["date"], "f": r["filename"],
                "fp": r["filepath"], "v": r["version_note"], "av": av(r)}

    data_js = json.dumps({
        "surfaces":    [ser_surface(r)    for r in surfaces],
        "alignments":  [ser_alignment(r)  for r in alignments],
        "pointclouds": [ser_pointcloud(r) for r in pointclouds],
        "scans":       [ser_scan(r)       for r in scans],
        "orthophotos": [ser_ortho(r)      for r in orthophotos],
        "tbcprojects": [ser_vce(r)        for r in tbcprojects],
        "csvfiles":    [ser_csv(r)        for r in csvfiles],
    }, ensure_ascii=False, separators=(",", ":"))

    tabs = []
    if surfaces:
        tabs.append(("surfaces",    f"Surfaces ({len(surfaces):,})"))
    if alignments:
        tabs.append(("alignments",  f"Alignments ({len(alignments):,})"))
    if pointclouds:
        tabs.append(("pointclouds", f"Point Clouds ({len(pointclouds):,})"))
    if scans:
        tabs.append(("scans",       f"Scans ({len(scans):,})"))
    if orthophotos:
        tabs.append(("orthophotos", f"Orthophotos ({len(orthophotos):,})"))
    if tbcprojects:
        tabs.append(("tbcprojects", f"TBC Projects ({len(tbcprojects):,})"))
    if csvfiles:
        tabs.append(("csvfiles", f"CSV Files ({len(csvfiles):,})"))
    has_map = bool(map_data and map_data["f"])
    if has_map:
        tabs.append(("map", "Map"))

    tab_buttons = "".join(
        f'<button id="btn-{tid}" class="tab-btn{" active" if i == 0 else ""}"'
        f' onclick="showTab(\'{tid}\')">{label}</button>'
        for i, (tid, label) in enumerate(tabs)
    )

    col_map = {
        "surfaces":    ["Name", "Points", "Faces", "Copies", "Date", "File", "Avail", "Path"],
        "alignments":  ["Name", "Length", ("Start Sta", "Start station"), ("End Sta", "End station"), "Copies", "Date", "File", "Avail", "Path"],
        "pointclouds": ["Name", "Points", ("Density", "Points per square metre (from LAS bounding box)"), "Size", "Copies", "Date", "File", "Avail", "Path"],
        "scans":       [("Scan", "Scan name — hover for the raw scan file"), ("Job", "JobXML (.jxl) job the scan was recorded in"), ("Station", "Instrument station — hover for its coordinates"), "Points", ("Scanned", "Date the scan was taken"), "Copies", "File", "Avail", "Path"],
        "orthophotos": ["Name", "Width", "Height", ("GSD", "Ground Sample Distance — pixel size at ground level"), "Size", "Copies", "Date", "File", "Avail", "Path"],
        "tbcprojects":  ["Name", "Size", "Copies", "Date", "File", "Avail", "Path"],
        "csvfiles":     ["Name", ("Points", "Data rows (excludes header)"), ("Cols", "Number of columns"), ("Codes", "Distinct feature codes — hover to list; type a code in the filter box to find matching files"), "Size", "Copies", "Date", "File", "Avail", "Path"],
    }

    panels = "".join(
        _MAP_PANEL if tid == "map" else _panel(tid, col_map[tid], i == 0)
        for i, (tid, _) in enumerate(tabs)
    )
    map_js = (f"const MAP={json.dumps(map_data, ensure_ascii=False, separators=(',', ':'))};\n{_MAP_JS}"
              if has_map else "")

    summary = " &amp; ".join(filter(None, [
        f"{len(surfaces):,} surfaces"        if surfaces    else "",
        f"{len(alignments):,} alignments"    if alignments  else "",
        f"{len(pointclouds):,} point clouds" if pointclouds else "",
        f"{len(scans):,} scans"              if scans       else "",
        f"{len(orthophotos):,} orthophotos"  if orthophotos else "",
        f"{len(tbcprojects):,} TBC projects" if tbcprojects else "",
        f"{len(csvfiles):,} CSV files"       if csvfiles    else "",
    ]))

    logo_tag = ""
    if logo_path is not None:
        try:
            logo_b64 = base64.b64encode(logo_path.read_bytes()).decode()
            logo_tag = f'<img src="data:image/png;base64,{logo_b64}" alt="logo">'
        except OSError:
            pass

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{_CSS}{_MAP_CSS if has_map else ""}</style>
</head>
<body>
<header>
  <div class="hdr-left">
    <h1>{title}</h1>
    <p>Generated {generated_at} &mdash; {summary} &mdash; {search_path}</p>
  </div>
  {logo_tag}
</header>
<div class="tabs">{tab_buttons}</div>
{panels}
<script>
const DATA={data_js};
{map_js}
{_JS}
</script>
</body>
</html>"""


# ── text output (legacy) ──────────────────────────────────────────────────────

def render_txt(rows_by_tag: dict[str, list[dict]], search_path: str) -> str:
    lines: list[str] = []
    for tag, rows in rows_by_tag.items():
        label = tag + "s"
        lines.append(f"{len(rows)} unique {label} in:\n  {search_path}\n")
        for r in rows:
            copies = f"  [×{r['copies']}]" if r["copies"] > 1 else ""
            vn     = f"  [{r['version_note']}]" if r["version_note"] else ""
            pts    = f"  ({r['pts']:,} pts)" if r["pts"] else ""
            lines.append(f"{r['name']}{pts}{copies}{vn}")
            lines.append(f"    [{r['date']}]  {r['filepath']}")
        lines.append("")
    return "\n".join(lines)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # The config file supplies defaults for some flags, so read --config first.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", metavar="FILE",
                     help="Settings file (TOML; default: survey_index.toml beside this "
                          "script, if present). See survey_index.example.toml")
    config_arg = pre.parse_known_args()[0].config
    config_path = Path(config_arg) if config_arg else (CONFIG_FILE if CONFIG_FILE.exists() else None)
    try:
        cfg = load_config(config_path)
    except (OSError, ValueError) as e:
        raise SystemExit(f"Settings file problem — {e}")
    apply_config(cfg)
    paths_cfg = cfg["paths"]

    parser = argparse.ArgumentParser(
        description="Index LandXML, LAZ, GeoTIFF, TBC, and CSV survey files in a directory tree.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[pre],
    )
    parser.add_argument("--path", required=True, metavar="DIR",
                        help="Root directory to search (required)")
    parser.add_argument("--title", default="Survey File Index",
                        help="Report title shown in the HTML header (default: 'Survey File Index')")
    parser.add_argument("--output-file", metavar="FILE",
                        help="Path for the HTML output file (default: index.html in --path)")
    parser.add_argument("--logo", metavar="FILE",
                        help="Optional path to a PNG logo to embed in the report header")
    parser.add_argument("--everything-url", default="http://localhost", metavar="URL",
                        help="Everything HTTP server URL (default: http://localhost)")
    parser.add_argument("--mode", choices=["surfaces", "alignments"], default="surfaces",
                        help="Element type to index (ignored when --batch)")
    parser.add_argument("--batch", action="store_true",
                        help="Index all file types in one pass")
    parser.add_argument("--sort", choices=["name", "date", "size"], default="name",
                        help="Initial sort order (default: name)")
    parser.add_argument("--output", choices=["html", "txt"], default="html")
    parser.add_argument("--no-hash-dedup",    action="store_true")
    parser.add_argument("--no-content-dedup", action="store_true")
    parser.add_argument("--download-cloud-xml-mb", type=float, default=0, metavar="MB",
                        help="Download (via OneDrive) and parse cloud-only LandXML (.xml) and "
                             "JobXML (.jxl) files up to this size in MB; larger ones are "
                             "skipped (default: 0 = skip all)")
    parser.add_argument("--prefer-path", metavar="FRAGMENT", action="append", default=[],
                        help="Prepend folder fragment to the config's priority list (repeatable)")
    parser.add_argument("--csv-under", action="append", default=None, metavar="FRAGMENT",
                        help="Only index CSV files whose path contains this folder fragment; "
                             "repeat for several folders (default from config: "
                             f"{paths_cfg['csv_under']!r}; \"\" = anywhere)")
    parser.add_argument("--control-under", default=paths_cfg["control_under"], metavar="FRAGMENT",
                        help="CSVs whose path contains this folder fragment are the project "
                             "control list; matching points are left out of CSV footprints "
                             f"(default from config: '{paths_cfg['control_under']}'; "
                             "\"\" = code/name matching only)")
    args = parser.parse_args()

    search_path = args.path
    eu = args.everything_url
    priority = args.prefer_path + list(paths_cfg["priority"])
    print(f"Settings: {config_path or 'built-in defaults (no survey_index.toml)'}\n")
    tags     = ["Surface", "Alignment"] if args.batch else (
               ["Surface"] if args.mode == "surfaces" else ["Alignment"])

    output_html = Path(args.output_file) if args.output_file else Path(search_path) / "index.html"
    output_txt  = Path(args.output_file) if args.output_file else Path(__file__).parent / "results.txt"
    logo_path   = Path(args.logo) if args.logo else None

    print(f"Searching for XML files in:\n  {search_path}\n")
    results = find_files(search_path, "xml", eu)
    print(f"Found {len(results)} XML file(s) — parsing for {', '.join(tags)}…\n")

    max_dl = int(args.download_cloud_xml_mb * 1024 * 1024)
    raw: list[tuple[str, dict, float, str | None]] = []
    n_downloaded = n_skipped = 0
    for item in results:
        path = str(Path(item["path"]) / item["name"])
        if not is_local(path):
            try:
                too_big = Path(path).stat().st_size > max_dl
            except OSError:
                too_big = True
            if too_big:
                n_skipped += 1
            else:
                n_downloaded += 1
        info, md5 = parse_file(path, tags, max_dl)
        if info:
            raw.append((path, info, file_mtime(path), md5))

    if n_downloaded:
        print(f"Downloaded {n_downloaded} cloud-only XML file(s) via OneDrive.")
    if n_skipped:
        print(f"Skipped {n_skipped} cloud-only XML file(s) over the "
              f"{args.download_cloud_xml_mb:g} MB --download-cloud-xml-mb limit.")
    print(f"Parsed {len(raw)} LandXML file(s) with matching elements.\n")

    rows_by_tag: dict[str, list[dict]] = {}
    for tag in tags:
        rows = build_rows(raw, tag, priority, args.no_hash_dedup, args.no_content_dedup)
        rows = sort_rows(rows, args.sort)
        rows_by_tag[tag] = rows
        print(f"  {tag}s: {len(rows)} unique entries")

    # Surface rows keep their canonical file path, so look their grids up by (path, name).
    grids = {(p, n): g for p, info, _, _ in raw for n, g in info.get("_grids", {}).items()}
    for r in rows_by_tag.get("Surface", []):
        g = grids.get((r["filepath"], r["name"]))
        r["cells"] = grid_cells(g) if g else []
    lines = {(p, n): ln for p, info, _, _ in raw for n, ln in info.get("_lines", {}).items()}
    for r in rows_by_tag.get("Alignment", []):
        r["line"] = lines.get((r["filepath"], r["name"]), [])

    control: set = set()
    if args.batch:
        print(f"\nSearching for LAZ/LAS files…")
        laz_results = find_files(search_path, "laz", eu) + find_files(search_path, "las", eu)
        print(f"Found {len(laz_results)} LAZ/LAS file(s) — reading headers…\n")
        laz_rows = build_laz_rows(laz_results, priority)
        laz_rows = sort_rows(laz_rows, args.sort)
        rows_by_tag["PointCloud"] = laz_rows
        print(f"  Point Clouds: {len(laz_rows)} unique entries")

        print(f"\nSearching for JXL files…")
        jxl_results = find_files(search_path, "jxl", eu)
        print(f"Found {len(jxl_results)} JXL file(s) — reading scan records…\n")
        scan_rows, n_jxl_skipped = build_scan_rows(jxl_results, priority, max_dl)
        scan_rows = sort_rows(scan_rows, args.sort)
        rows_by_tag["Scan"] = scan_rows
        print(f"  Scans: {len(scan_rows)} unique entries "
              f"({sum(1 for r in scan_rows if r['poly'])} located)")
        if n_jxl_skipped:
            print(f"  Skipped {n_jxl_skipped} cloud-only JXL file(s) over the "
                  f"{args.download_cloud_xml_mb:g} MB --download-cloud-xml-mb limit.")

        print(f"\nSearching for TIFF files…")
        tif_results = find_files(search_path, "tif", eu) + find_files(search_path, "tiff", eu)
        print(f"Found {len(tif_results)} TIFF file(s) — reading headers…\n")
        ortho_rows = build_ortho_rows(tif_results, priority)
        ortho_rows = sort_rows(ortho_rows, args.sort)
        rows_by_tag["Ortho"] = ortho_rows
        print(f"  Orthophotos: {len(ortho_rows)} unique entries")

        print(f"\nSearching for VCE files…")
        vce_results = find_files(search_path, "vce", eu)
        print(f"Found {len(vce_results)} VCE file(s)…\n")
        vce_rows = build_vce_rows(vce_results, priority)
        vce_rows = sort_rows(vce_rows, args.sort)
        rows_by_tag["TBC"] = vce_rows
        print(f"  TBC Projects: {len(vce_rows)} unique entries")

        print(f"\nSearching for CSV files…")
        csv_results = find_files(search_path, "csv", eu)
        if args.control_under:
            frag = args.control_under.lower()
            ctrl_paths = [str(Path(it["path"]) / it["name"]) for it in csv_results
                          if frag in str(Path(it["path"]) / it["name"]).lower()]
            control = load_control(ctrl_paths)
            print(f"Loaded {len(control)} control point(s) from {len(ctrl_paths)} CSV(s) "
                  f"under '{args.control_under}'.")
        csv_under = args.csv_under if args.csv_under is not None else paths_cfg["csv_under"]
        if isinstance(csv_under, str):
            csv_under = [csv_under]
        frags = [f.lower() for f in csv_under if f]
        if frags:
            kept = [it for it in csv_results
                    if any(f in str(Path(it["path"]) / it["name"]).lower() for f in frags)]
            print(f"Found {len(csv_results)} CSV file(s); {len(kept)} under "
                  f"{' or '.join(repr(f).replace(chr(92) * 2, chr(92)) for f in csv_under if f)} — reading rows…\n")
            csv_results = kept
        else:
            print(f"Found {len(csv_results)} CSV file(s) — reading rows…\n")
        csv_rows = build_csv_rows(csv_results, priority, control)
        csv_rows = sort_rows(csv_rows, args.sort)
        rows_by_tag["CSV"] = csv_rows
        print(f"  CSV Files: {len(csv_rows)} unique entries "
              f"({sum(r['excluded'] for r in csv_rows):,} control points left out of footprints)")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    if args.output == "html":
        map_data = build_map_data(rows_by_tag, control)
        if cfg["map"]["basemap"]:
            src = find_basemap(cfg["map"]["basemap"], search_path)
            if src is None:
                print(f"  [warn] Map background: nothing matches '{cfg['map']['basemap']}'.")
            else:
                # Tiles sit beside the report (index.html → index.basemap\), referenced relatively.
                tiles = output_html.with_suffix(".basemap")
                bm = build_basemap(src, tiles, float(cfg["map"]["basemap_resolution"]))
                if bm:
                    oe, on = map_data["o"]
                    map_data["bm"] = {"dir": tiles.name, "name": bm["name"],
                                      "x0": round(bm["x0"] - oe, 2), "y0": round(bm["y0"] - on, 2),
                                      "gsd": bm["gsd"], "tile": bm["tile"], "levels": bm["levels"]}
        html_out = render_html(rows_by_tag, search_path, generated_at, args.title, logo_path, map_data)
        output_html.write_text(html_out, encoding="utf-8")
        print(f"\nHTML index written to: {output_html}")
    else:
        txt_out = render_txt(rows_by_tag, search_path)
        output_txt.write_text(txt_out, encoding="utf-8")
        print(f"\nResults written to: {output_txt}")

    # Footprints sit beside the report (index.html → index.footprints.json).
    footprints = build_footprints(rows_by_tag)
    fp_path = (output_html if args.output == "html" else output_txt).with_suffix(".footprints.json")
    fp_path.write_text(json.dumps({"generated": generated_at, "cell": FOOTPRINT_CELL,
                                   "files": footprints}, separators=(",", ":")), encoding="utf-8")
    print(f"Footprints for {len(footprints)} file(s) written to: {fp_path} "
          f"({fp_path.stat().st_size / 1024:,.0f} KB)")


if __name__ == "__main__":
    main()

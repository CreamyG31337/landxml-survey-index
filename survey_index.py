"""
LandXML Survey Index — indexes geospatial survey files and writes a self-contained
sortable/pageable HTML report (or plain text with --output txt).

File types indexed: LandXML (.xml) surfaces & alignments, LAZ/LAS point clouds,
GeoTIFF orthophotos, and TBC project files (.vce).

File discovery uses Everything (voidtools HTTP API) when available, and falls back
to a directory walk automatically if Everything is not installed.

Deduplication (all active by default):
  Hash dedup:     identical files (same MD5 prefix) collapse to the most canonical
                  copy; a ×N count is shown.
  Path priority:  canonical path chosen by folder-preference order.
                  Default: 02-DESIGN > 03-ASBUILTS > 05-QC SURVEY DATA >
                           07-DATALOGGER BACKUP > 01-OUTPUT >
                           06-WORKING DATA\\TBC > 06-WORKING DATA
                  Override with --prefer-path FRAGMENT (repeatable, prepended).
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
import os
import struct
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DEFAULT_PATH_PRIORITY = [
    "02-DESIGN",
    "03-ASBUILTS",
    "05-QC SURVEY DATA",
    "07-DATALOGGER BACKUP",
    "01-OUTPUT",
    r"06-WORKING DATA\TBC",
    "06-WORKING DATA",
]
PAGE_SIZE = 50


# ── data access ───────────────────────────────────────────────────────────────

def query_everything(search: str, max_results: int = 2000, url: str = "http://localhost") -> list[dict]:
    params = urllib.parse.urlencode({
        "s": search, "j": "1", "path_column": "1", "count": str(max_results),
    })
    with urllib.request.urlopen(f"{url}/?{params}", timeout=10) as r:
        return json.loads(r.read().decode())["results"]


def scan_files(search_path: str, ext: str) -> list[dict]:
    """Walk directory tree; return [{path, name}] matching Everything's format."""
    results = []
    for root, _dirs, files in os.walk(search_path):
        for fname in files:
            if fname.lower().endswith("." + ext.lower()):
                results.append({"path": root, "name": fname})
    return results


_everything_ok: bool | None = None  # None = untested, True = working, False = unavailable


def find_files(search_path: str, ext: str, everything_url: str = "http://localhost", max_results: int = 2000) -> list[dict]:
    """Query Everything if available; fall back to os.walk silently after first failure."""
    global _everything_ok
    if _everything_ok is not False:
        try:
            results = query_everything(f'ext:{ext} path:"{search_path}"', max_results, everything_url)
            _everything_ok = True
            return results
        except Exception:
            if _everything_ok is None:
                print("  [info] Everything HTTP API not reachable — using directory scan (slower).")
            _everything_ok = False
    return scan_files(search_path, ext)


def parse_file(
    filepath: str, tags: list[str]
) -> tuple[dict[str, list[tuple[str, int]]], str | None]:
    """Read file once; return ({tag: [(name, pts, faces)]}, md5). Non-LandXML → ({}, None)."""
    try:
        if not is_local(filepath):
            name = Path(filepath).stem
            return {tag: [(name, 0, 0, 0.0, 0.0)] for tag in tags}, None
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
                 sum(1 for _ in el.iter(f"{pfx}P")),
                 sum(1 for _ in el.iter(f"{pfx}F")),
                 float(el.get("length", 0) or 0),
                 float(el.get("staStart", 0) or 0))
                for el in root.iter(f"{pfx}{tag}")
            ]
            if items:
                out[tag] = items
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
    """Read LAS/LAZ header; return {"pts": N, "density": pts/m²} or None."""
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
        if len(hdr) >= 211:
            xmax, xmin, ymax, ymin = struct.unpack_from("<4d", hdr, 179)
            area = (xmax - xmin) * (ymax - ymin)
            if area > 0 and pts > 0:
                density = pts / area
        return {"pts": pts, "density": density}
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
        h = partial_hash(path) if local else None
        if h:
            hash_groups[h].append((path, pts, density, mtime, size))
        else:
            no_hash.append((path, pts, density, mtime, size, 1))

    files: list[tuple] = list(no_hash)
    for group in hash_groups.values():
        paths = [p for p, _, _, _, _ in group]
        canon = best_path(paths, priority)
        canon_pts, canon_density, canon_t, canon_sz = next(
            (pts, d, t, sz) for p, pts, d, t, sz in group if p == canon
        )
        files.append((canon, canon_pts, canon_density, canon_t, canon_sz, len(group)))

    rows = []
    for path, pts, density, mtime, size, copies in files:
        rows.append({
            "name":         Path(path).stem,
            "pts":          pts,
            "density":      density,
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
  for(const id of['surfaces','alignments','pointclouds','orthophotos','tbcprojects']){
    const d=DATA[id];if(!d||!d.length)continue;
    T[id]={rows:d,fil:d,pg:0,col:null,dir:1};render(id);
  }
}
function filt(id){
  const q=document.getElementById('q-'+id).value.toLowerCase();
  T[id].fil=q?T[id].rows.filter(r=>(r.n+' '+r.f).toLowerCase().includes(q)):T[id].rows;
  T[id].pg=0;render(id);
}
function srt(id,c){const t=T[id];t.dir=t.col===c?-t.dir:1;t.col=c;t.pg=0;render(id);}
function go(id,p){T[id].pg=p;render(id);}
const KEYS={surfaces:['n','p','fa','c','d','f','av'],alignments:['n','len','sta','sta_e','c','d','f','av'],pointclouds:['n','p','den','sz','c','d','f','av'],orthophotos:['n','w','h','gsd','sz','c','d','f','av'],tbcprojects:['n','sz','c','d','f','av']};
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
  if(id==='tbcprojects')
    return`<tr><td>${nm}</td><td class="num">${fmtsz(r.sz)}</td><td class="num">${cp}</td><td>${r.d}</td>${fn}${av}${lk}</tr>`;
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
}
function x(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
window.addEventListener('load',init);
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
) -> str:
    surfaces     = rows_by_tag.get("Surface", [])
    alignments   = rows_by_tag.get("Alignment", [])
    pointclouds  = rows_by_tag.get("PointCloud", [])
    orthophotos  = rows_by_tag.get("Ortho", [])
    tbcprojects  = rows_by_tag.get("TBC", [])

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
    def ser_vce(r):
        return {"n": r["name"], "sz": r["size"], "c": r["copies"],
                "d": r["date"], "f": r["filename"], "fp": r["filepath"], "v": r["version_note"], "av": av(r)}

    data_js = json.dumps({
        "surfaces":    [ser_surface(r)    for r in surfaces],
        "alignments":  [ser_alignment(r)  for r in alignments],
        "pointclouds": [ser_pointcloud(r) for r in pointclouds],
        "orthophotos": [ser_ortho(r)      for r in orthophotos],
        "tbcprojects": [ser_vce(r)        for r in tbcprojects],
    }, ensure_ascii=False, separators=(",", ":"))

    tabs = []
    if surfaces:
        tabs.append(("surfaces",    f"Surfaces ({len(surfaces):,})"))
    if alignments:
        tabs.append(("alignments",  f"Alignments ({len(alignments):,})"))
    if pointclouds:
        tabs.append(("pointclouds", f"Point Clouds ({len(pointclouds):,})"))
    if orthophotos:
        tabs.append(("orthophotos", f"Orthophotos ({len(orthophotos):,})"))
    if tbcprojects:
        tabs.append(("tbcprojects", f"TBC Projects ({len(tbcprojects):,})"))

    tab_buttons = "".join(
        f'<button id="btn-{tid}" class="tab-btn{" active" if i == 0 else ""}"'
        f' onclick="showTab(\'{tid}\')">{label}</button>'
        for i, (tid, label) in enumerate(tabs)
    )

    col_map = {
        "surfaces":    ["Name", "Points", "Faces", "Copies", "Date", "File", "Avail", "Path"],
        "alignments":  ["Name", "Length", ("Start Sta", "Start station"), ("End Sta", "End station"), "Copies", "Date", "File", "Avail", "Path"],
        "pointclouds": ["Name", "Points", ("Density", "Points per square metre (from LAS bounding box)"), "Size", "Copies", "Date", "File", "Avail", "Path"],
        "orthophotos": ["Name", "Width", "Height", ("GSD", "Ground Sample Distance — pixel size at ground level"), "Size", "Copies", "Date", "File", "Avail", "Path"],
        "tbcprojects":  ["Name", "Size", "Copies", "Date", "File", "Avail", "Path"],
    }

    panels = "".join(
        _panel(tid, col_map[tid], i == 0)
        for i, (tid, _) in enumerate(tabs)
    )

    summary = " &amp; ".join(filter(None, [
        f"{len(surfaces):,} surfaces"        if surfaces    else "",
        f"{len(alignments):,} alignments"    if alignments  else "",
        f"{len(pointclouds):,} point clouds" if pointclouds else "",
        f"{len(orthophotos):,} orthophotos"  if orthophotos else "",
        f"{len(tbcprojects):,} TBC projects" if tbcprojects else "",
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
<style>{_CSS}</style>
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
    parser = argparse.ArgumentParser(
        description="Index LandXML, LAZ, GeoTIFF, and TBC survey files in a directory tree.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    parser.add_argument("--prefer-path", metavar="FRAGMENT", action="append", default=[],
                        help="Prepend folder fragment to priority list (repeatable)")
    args = parser.parse_args()

    search_path = args.path
    eu = args.everything_url
    priority = args.prefer_path + DEFAULT_PATH_PRIORITY
    tags     = ["Surface", "Alignment"] if args.batch else (
               ["Surface"] if args.mode == "surfaces" else ["Alignment"])

    output_html = Path(args.output_file) if args.output_file else Path(search_path) / "index.html"
    output_txt  = Path(args.output_file) if args.output_file else Path(__file__).parent / "results.txt"
    logo_path   = Path(args.logo) if args.logo else None

    print(f"Searching for XML files in:\n  {search_path}\n")
    results = find_files(search_path, "xml", eu)
    print(f"Found {len(results)} XML file(s) — parsing for {', '.join(tags)}…\n")

    raw: list[tuple[str, dict, float, str | None]] = []
    for item in results:
        path = str(Path(item["path"]) / item["name"])
        info, md5 = parse_file(path, tags)
        if info:
            raw.append((path, info, file_mtime(path), md5))

    print(f"Parsed {len(raw)} LandXML file(s) with matching elements.\n")

    rows_by_tag: dict[str, list[dict]] = {}
    for tag in tags:
        rows = build_rows(raw, tag, priority, args.no_hash_dedup, args.no_content_dedup)
        rows = sort_rows(rows, args.sort)
        rows_by_tag[tag] = rows
        print(f"  {tag}s: {len(rows)} unique entries")

    if args.batch:
        print(f"\nSearching for LAZ files…")
        laz_results = find_files(search_path, "laz", eu)
        print(f"Found {len(laz_results)} LAZ file(s) — reading headers…\n")
        laz_rows = build_laz_rows(laz_results, priority)
        laz_rows = sort_rows(laz_rows, args.sort)
        rows_by_tag["PointCloud"] = laz_rows
        print(f"  Point Clouds: {len(laz_rows)} unique entries")

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

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    if args.output == "html":
        html_out = render_html(rows_by_tag, search_path, generated_at, args.title, logo_path)
        output_html.write_text(html_out, encoding="utf-8")
        print(f"\nHTML index written to: {output_html}")
    else:
        txt_out = render_txt(rows_by_tag, search_path)
        output_txt.write_text(txt_out, encoding="utf-8")
        print(f"\nResults written to: {output_txt}")


if __name__ == "__main__":
    main()

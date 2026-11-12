"""Tests for survey_index.py on small synthetic files (no project data, no Everything service).

Run from the repo folder:  python -m pytest
"""
import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import survey_index as si  # noqa: E402

E0, N0 = 500000.0, 5000000.0   # UTM-sized: 6-digit easting, 7-digit northing (no real site)


@pytest.fixture(autouse=True)
def default_config():
    si.apply_config(si.DEFAULT_CONFIG)
    yield
    si.apply_config(si.DEFAULT_CONFIG)


def write(tmp_path, name, text, encoding="utf-8"):
    p = tmp_path / name
    p.write_text(text, encoding=encoding)
    return str(p)


# ---------------------------------------------------------------- CSV parsing

def test_csv_with_header(tmp_path):
    p = write(tmp_path, "h.csv", "Point,Northing,Easting,Elevation,Code\n"
              f"1,{N0},{E0},200.5,TOR\n2,{N0 + 12},{E0 + 7},201.0,OG\n3,{N0 + 1},{E0 + 1},199.0,TOR\n")
    m = si.parse_csv_meta(p)
    assert m["rows"] == 3 and m["cols"] == 5
    assert m["bbox"] == (E0, N0, E0 + 7, N0 + 12)
    assert (m["zmin"], m["zmax"]) == (199.0, 201.0)
    assert m["codes"] == ["OG", "TOR"] and set(m["terms"]) == {"OG", "TOR"}
    assert sum(c for _, _, c in m["cells"]) == 3 and m["excluded"] == 0


def test_headerless_pnezd_and_penzd_are_both_placed(tmp_path):
    pnezd = write(tmp_path, "a.csv", f"1,{N0},{E0},200,TOR\n2,{N0 + 5},{E0 + 3},201,TOR\n")
    penzd = write(tmp_path, "b.csv", f"1,{E0},{N0},200,TOR\n2,{E0 + 3},{N0 + 5},201,TOR\n")
    for p in (pnezd, penzd):
        m = si.parse_csv_meta(p)
        assert m["rows"] == 2 and m["bbox"] == (E0, N0, E0 + 3, N0 + 5), p
        assert m["codes"] == ["TOR"] and m["zmax"] == 201


def test_text_point_name_first_row_is_not_a_header(tmp_path):
    p = write(tmp_path, "s.csv", f"S2,{N0},{E0},200,TOR\n3,{N0 + 1},{E0 + 1},200,TOR\n")
    assert si.parse_csv_meta(p)["rows"] == 2


def test_semicolon_delimiter_and_bom(tmp_path):
    p = write(tmp_path, "sc.csv", f"P;N;E;Z;D\n1;{N0};{E0};200;OG\n2;{N0 + 1};{E0 + 2};201;OG\n",
              encoding="utf-8-sig")
    m = si.parse_csv_meta(p)
    assert m["rows"] == 2 and m["bbox"] == (E0, N0, E0 + 2, N0 + 1)


def test_empty_csv(tmp_path):
    assert si.parse_csv_meta(write(tmp_path, "e.csv", ""))["rows"] == 0


def test_control_by_code_and_name_is_excluded(tmp_path):
    rows = [("CP 2", N0 + 900, E0 + 900, "TOR"),       # control by name prefix
            ("7", N0 + 800, E0 + 800, "MON2"),          # control by code word
            ("8", N0 + 700, E0 + 700, "TEST-001"),      # whole-code control
            ("9", N0 + 1, E0 + 1, "DENSITY-TEST"),      # not control
            ("10", N0, E0, "TOR")]
    p = write(tmp_path, "c.csv", "Point,Northing,Easting,Elevation,Code\n"
              + "".join(f"{a},{n},{e},200,{c}\n" for a, n, e, c in rows))
    m = si.parse_csv_meta(p)
    assert m["excluded"] == 3
    assert m["bbox"] == (E0, N0, E0 + 1, N0 + 1)
    assert m["codes"] == sorted({c for *_, c in rows})     # codes still listed


def test_control_by_coordinate(tmp_path):
    ctl = write(tmp_path, "control.csv", f"Point,Northing,Easting,Elevation,Code\nX1,{N0 + 500},{E0 + 500},210,\n")
    control = si.load_control([ctl])
    assert si.near_control(control, E0 + 500.05, N0 + 499.95)
    assert not si.near_control(control, E0 + 501, N0 + 500)
    data = write(tmp_path, "d.csv", f"1,{N0 + 500.03},{E0 + 500.02},210,STAKE\n2,{N0},{E0},200,STAKE\n")
    m = si.parse_csv_meta(data, control=control)
    assert m["excluded"] == 1 and m["bbox"] == (E0, N0, E0, N0)


def test_high_cardinality_text_column_left_out_of_terms(tmp_path):
    lines = ["Point,Northing,Easting,Elevation,Code,Note"]
    lines += [f"{i},{N0 + i},{E0},200,{'A' if i % 2 else 'B'},note {i}" for i in range(si._CSV_COL_CARD + 5)]
    m = si.parse_csv_meta(write(tmp_path, "n.csv", "\n".join(lines) + "\n"))
    assert set(m["terms"]) == {"A", "B"}


def test_points_collects_every_coordinate(tmp_path):
    pts = []
    si.parse_csv_meta(write(tmp_path, "p.csv", f"1,{N0},{E0},1,CP\n2,{N0 + 1},{E0 + 1},1,TOR\n"), points=pts)
    assert pts == [(E0, N0), (E0 + 1, N0 + 1)]       # control points included here


# ---------------------------------------------------------------- footprints

def test_grid_cells_and_bbox():
    g = {}
    for e, n in [(1, 1), (2, 3), (12, 1), (-1, -1)]:
        si.grid_add(g, e, n)
    si.grid_add(g, float("nan"), 0)
    cells = si.grid_cells(g)
    assert cells == [[-1, -1, 1], [0, 0, 2], [2, 0, 1]]           # row-major
    assert si.cells_bbox(cells) == (-5.0, -5.0, 15.0, 5.0)
    assert si.cells_bbox([]) is None


def test_build_footprints():
    rows = {"CSV": [{"name": "a.csv", "filepath": "X:/a.csv", "date": "2026-01-01",
                     "cells": [[0, 0, 3]], "excluded": 2}],
            "PointCloud": [{"name": "f.laz", "filepath": "X:/f.laz", "date": "2026-01-02",
                            "bbox": (1.23456, 2, 3, 4)}],
            "Surface": [{"name": "empty", "filepath": "X:/s.xml", "date": "", "cells": []}]}
    fps = si.build_footprints(rows)
    assert [f["type"] for f in fps] == ["csv", "pointcloud"]
    assert fps[0]["bbox"] == [0, 0, 5.0, 5.0] and fps[0]["excluded"] == 2
    assert fps[1]["bbox"] == [1.235, 2, 3, 4] and "cells" not in fps[1]


def test_convex_hull_ccw():
    hull = si.convex_hull([(0, 0), (4, 0), (4, 4), (0, 4), (2, 2), (4, 0)])
    assert hull == [(0, 0), (4, 0), (4, 4), (0, 4)]


# ---------------------------------------------------------------- binary headers

def test_las_header(tmp_path):
    h = bytearray(375)
    h[:4] = b"LASF"
    h[24], h[25] = 1, 2
    struct.pack_into("<I", h, 107, 1000)
    struct.pack_into("<6d", h, 179, E0 + 10, E0, N0 + 20, N0, 250.0, 200.0)   # xmax xmin ymax ymin zmax zmin
    p = tmp_path / "c.las"
    p.write_bytes(bytes(h))
    r = si.parse_laz_header(str(p))
    assert r == {"pts": 1000, "density": 5.0, "bbox": (E0, N0, E0 + 10, N0 + 20)}
    (tmp_path / "x.las").write_bytes(b"nope" * 100)
    assert si.parse_laz_header(str(tmp_path / "x.las")) is None


def test_geotiff_header(tmp_path):
    w, h, gsd = 200, 100, 0.05
    entries = [(256, 3, 1, w), (257, 3, 1, h), (33550, 12, 3, None), (33922, 12, 6, None)]
    ifd_off = 8
    data_off = ifd_off + 2 + len(entries) * 12 + 4
    scale = struct.pack("<3d", gsd, gsd, 0.0)
    tie = struct.pack("<6d", 0, 0, 0, E0, N0, 0)
    offs = {33550: data_off, 33922: data_off + len(scale)}
    ifd = struct.pack("<H", len(entries))
    for tag, typ, cnt, val in entries:
        ifd += struct.pack("<HHI", tag, typ, cnt)
        ifd += struct.pack("<HH", val, 0) if typ == 3 else struct.pack("<I", offs[tag])
    p = tmp_path / "o.tif"
    p.write_bytes(b"II" + struct.pack("<HI", 42, ifd_off) + ifd + b"\0\0\0\0" + scale + tie)
    r = si.parse_tiff_header(str(p))
    assert (r["width"], r["height"], r["gsd"]) == (w, h, gsd)
    assert r["bbox"] == pytest.approx((E0, N0 - h * gsd, E0 + w * gsd, N0))


# ---------------------------------------------------------------- config and paths

def test_load_config(tmp_path):
    good = tmp_path / "ok.toml"
    good.write_text('[paths]\ncsv_under = ["05-QC", \'06-WORKING\\TBC\']\n[footprints]\ncell_size = 2.0\n',
                    encoding="utf-8-sig")     # BOM, as Notepad writes it
    cfg = si.load_config(good)
    assert cfg["paths"]["csv_under"] == ["05-QC", "06-WORKING\\TBC"] and cfg["footprints"]["cell_size"] == 2.0
    assert cfg["control"] == si.DEFAULT_CONFIG["control"]
    bad = tmp_path / "bad.toml"
    bad.write_text("[paths]\ncsv_undr = 'x'\n")
    with pytest.raises(ValueError, match="csv_undr"):
        si.load_config(bad)


def test_apply_config_changes_cell_size():
    cfg = si.load_config(None)
    cfg["footprints"]["cell_size"] = 2.0
    si.apply_config(cfg)
    g = {}
    si.grid_add(g, 3.0, 3.0)
    assert list(g) == [(1, 1)]


def test_priority():
    paths = [r"C:\old\copy\f.xml", r"S:\01-OUTPUT\f.xml", r"S:\03-ASBUILTS\f.xml"]
    assert si.best_path(paths, ["03-asbuilts", "01-output"]) == paths[2]
    assert si.priority_score(paths[0], ["03-asbuilts"]) == 1


# ---------------------------------------------------------------- end to end

LANDXML = """<?xml version="1.0"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">
 <Surfaces><Surface name="OG"><Definition surfType="TIN"><Pnts>
  <P id="1">{n} {e} 200</P><P id="2">{n} {e2} 201</P><P id="3">{n2} {e} 202</P>
 </Pnts><Faces><F>1 2 3</F></Faces></Definition></Surface></Surfaces>
</LandXML>
"""


def test_main_batch_writes_report_and_footprints(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "QC").mkdir(parents=True)
    (data / "QC" / "shots.csv").write_text(f"1,{N0},{E0},200,TOR\n2,{N0 + 3},{E0 + 3},201,TOR\n")
    (data / "og.xml").write_text(LANDXML.format(n=N0, e=E0, e2=E0 + 10, n2=N0 + 10))
    cfg = tmp_path / "empty.toml"
    cfg.write_text("")
    out = tmp_path / "report" / "index.html"
    out.parent.mkdir()
    monkeypatch.setattr(sys, "argv", ["survey_index.py", "--config", str(cfg), "--path", str(data), "--batch",
                                      "--output-file", str(out), "--everything-url", "http://127.0.0.1:9"])
    si.main()
    assert "shots.csv" in out.read_text(encoding="utf-8")
    fp = json.loads(out.with_suffix(".footprints.json").read_text(encoding="utf-8"))
    kinds = {f["type"]: f for f in fp["files"]}
    assert fp["cell"] == 5.0 and {"csv", "surface"} <= kinds.keys()
    assert kinds["csv"]["bbox"] == [E0, N0, E0 + 3, N0 + 3]

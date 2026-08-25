# Graph Report - file-finder  (2026-08-25)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 50 nodes · 101 edges · 8 communities (7 shown, 1 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `1d49ccc3`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Community 0
- Community 1
- Community 2
- Community 3
- Community 4
- Community 5
- Community 6

## God Nodes (most connected - your core abstractions)
1. `main()` - 13 edges
2. `build_laz_rows()` - 10 edges
3. `build_ortho_rows()` - 10 edges
4. `build_csv_rows()` - 9 edges
5. `build_vce_rows()` - 9 edges
6. `folder_url()` - 8 edges
7. `best_path()` - 7 edges
8. `build_rows()` - 7 edges
9. `is_local()` - 7 edges
10. `fmt_date()` - 6 edges

## Surprising Connections (you probably didn't know these)
- `main()` --calls--> `Path`  [EXTRACTED]
  survey_index.py →   _Bridges community 0 → community 2_
- `main()` --calls--> `build_csv_rows()`  [EXTRACTED]
  survey_index.py → survey_index.py  _Bridges community 0 → community 1_
- `main()` --calls--> `build_laz_rows()`  [EXTRACTED]
  survey_index.py → survey_index.py  _Bridges community 0 → community 3_
- `main()` --calls--> `build_ortho_rows()`  [EXTRACTED]
  survey_index.py → survey_index.py  _Bridges community 0 → community 5_
- `build_laz_rows()` --calls--> `best_path()`  [EXTRACTED]
  survey_index.py → survey_index.py  _Bridges community 1 → community 3_

## Import Cycles
- None detected.

## Communities (8 total, 1 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.27
Nodes (11): find_files(), main(), _panel(), query_everything(), LandXML Survey Index — indexes geospatial survey files and writes a self-…, Walk directory tree; return [{path, name}] matching Everything's format., Query Everything if available; fall back to os.walk silently after first…, render_html() (+3 more)

### Community 1 - "Community 1"
Cohesion: 0.25
Nodes (11): best_path(), build_csv_rows(), build_rows(), build_vce_rows(), fmt_date(), folder_url(), priority_score(), Convert a Windows path to a file:// URI for the containing folder. (+3 more)

### Community 2 - "Community 2"
Cohesion: 0.29
Nodes (8): Path, file_mtime(), is_local(), parse_file(), Vertex count for a Surface element. Triangulated (TIN) surfaces store vertices…, Read file once; return ({tag: [(name, pts, faces)]}, md5). Non-LandXML → ({},…, Return False if this is an OneDrive cloud-only file (not downloaded locally)., surface_point_count()

### Community 3 - "Community 3"
Cohesion: 0.33
Nodes (6): build_laz_rows(), parse_laz_header(), partial_hash(), Read LAS/LAZ header; return {"pts": N, "density": pts/m²} or None., MD5 of first nbytes — fast proxy for large binary files., Index LAZ/LAS files: hash-dedup by first 64 KB, path priority.

### Community 4 - "Community 4"
Cohesion: 0.40
Nodes (5): _is_number(), _numcount(), parse_csv_meta(), Read a CSV survey/point file. Returns {"rows": data-row count, "cols": column…, Count fields in a row that parse as numbers (used for header detection).

### Community 5 - "Community 5"
Cohesion: 0.50
Nodes (4): build_ortho_rows(), parse_tiff_header(), Read TIFF/BigTIFF IFD for dimensions and GeoTIFF spatial tags (33550+33922 or…, Index TIFF/GeoTIFF orthophotos: hash-dedup by first 64 KB, path priority.

## Knowledge Gaps
- **1 isolated node(s):** `C:\Users\lcolton1\AppData\Roaming\uv\tools\graphifyy\Scripts\python.exe`
  These have ≤1 connection - possible missing edges or undocumented components.
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `build_laz_rows()` connect `Community 3` to `Community 0`, `Community 1`, `Community 2`?**
  _High betweenness centrality (0.054) - this node is a cross-community bridge._
- **Why does `build_ortho_rows()` connect `Community 5` to `Community 0`, `Community 1`, `Community 2`, `Community 3`?**
  _High betweenness centrality (0.054) - this node is a cross-community bridge._
- **Why does `build_csv_rows()` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`?**
  _High betweenness centrality (0.050) - this node is a cross-community bridge._
- **What connects `C:\Users\lcolton1\AppData\Roaming\uv\tools\graphifyy\Scripts\python.exe` to the rest of the system?**
  _1 weakly-connected nodes found - possible documentation gaps or missing edges._
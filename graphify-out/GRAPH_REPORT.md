# Graph Report - file-finder  (2026-10-07)

## Corpus Check
- 8 files · ~25,166 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 2 file(s) not represented in the graph (top: (none) 1, .bat 1)

## Summary
- 85 nodes · 131 edges · 13 communities (8 shown, 5 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `5332f31d`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- find_files
- build_laz_rows
- is_local
- survey_index.py
- parse_csv_meta
- parse_tiff_header
- graphify
- LandXML Survey Index
- CLAUDE.md
- copilot-instructions.md
- render_html
- main

## God Nodes (most connected - your core abstractions)
1. `main()` - 12 edges
2. `render_html()` - 11 edges
3. `build_laz_rows()` - 9 edges
4. `build_ortho_rows()` - 9 edges
5. `build_vce_rows()` - 8 edges
6. `build_csv_rows()` - 8 edges
7. `av()` - 8 edges
8. `is_local()` - 7 edges
9. `best_path()` - 7 edges
10. `folder_url()` - 7 edges

## Surprising Connections (you probably didn't know these)
- `main()` --calls--> `find_files()`  [EXTRACTED]
  survey_index.py → survey_index.py  _Bridges community 0 → community 12_
- `main()` --calls--> `parse_file()`  [EXTRACTED]
  survey_index.py → survey_index.py  _Bridges community 2 → community 12_
- `build_laz_rows()` --calls--> `is_local()`  [EXTRACTED]
  survey_index.py → survey_index.py  _Bridges community 2 → community 1_
- `av()` --calls--> `is_local()`  [EXTRACTED]
  survey_index.py → survey_index.py  _Bridges community 2 → community 11_
- `build_ortho_rows()` --calls--> `parse_tiff_header()`  [EXTRACTED]
  survey_index.py → survey_index.py  _Bridges community 5 → community 1_

## Import Cycles
- None detected.

## Communities (13 total, 5 thin omitted)

### Community 0 - "find_files"
Cohesion: 0.33
Nodes (6): find_files(), query_everything(), Query Everything if available; fall back to os.walk silently after first…, Return every match, paging through the API so large result sets aren't…, Walk directory tree; return [{path, name}] matching Everything's format., scan_files()

### Community 1 - "build_laz_rows"
Cohesion: 0.17
Nodes (19): best_path(), build_csv_rows(), build_laz_rows(), build_ortho_rows(), build_rows(), build_vce_rows(), fmt_date(), folder_url() (+11 more)

### Community 2 - "is_local"
Cohesion: 0.33
Nodes (6): is_local(), parse_file(), Vertex count for a Surface element. Triangulated (TIN) surfaces store vertices…, Read file once; return ({tag: [(name, pts, faces)]}, md5). Non-LandXML → ({},…, Return False if this is an OneDrive cloud-only file (not downloaded locally)., surface_point_count()

### Community 3 - "survey_index.py"
Cohesion: 0.14
Nodes (13): argparse, base64, collections, datetime, hashlib, json, os, pathlib (+5 more)

### Community 4 - "parse_csv_meta"
Cohesion: 0.40
Nodes (6): _is_number(), _numcount(), parse_csv_meta(), consume(), Read a CSV survey/point file. Returns {"rows": data-row count, "cols": column…, Count fields in a row that parse as numbers (used for header detection).

### Community 8 - "LandXML Survey Index"
Cohesion: 0.22
Nodes (8): All options, Deduplication, File types indexed, LandXML Survey Index, Requirements, Scheduled indexing, Screenshots, Usage

### Community 11 - "render_html"
Cohesion: 0.29
Nodes (10): Path, _panel(), render_html(), av(), ser_alignment(), ser_csv(), ser_ortho(), ser_pointcloud() (+2 more)

### Community 12 - "main"
Cohesion: 0.50
Nodes (4): file_mtime(), main(), render_txt(), sort_rows()

## Knowledge Gaps
- **9 isolated node(s):** `C:\Users\lcolton1\AppData\Roaming\uv\tools\graphifyy\Scripts\python.exe`, `graphify`, `graphify`, `Screenshots`, `File types indexed` (+4 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 46 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **5 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `render_html()` connect `render_html` to `survey_index.py`, `main`?**
  _High betweenness centrality (0.122) - this node is a cross-community bridge._
- **Why does `is_local()` connect `is_local` to `render_html`, `build_laz_rows`, `survey_index.py`?**
  _High betweenness centrality (0.034) - this node is a cross-community bridge._
- **Why does `main()` connect `main` to `find_files`, `build_laz_rows`, `is_local`, `survey_index.py`, `render_html`?**
  _High betweenness centrality (0.032) - this node is a cross-community bridge._
- **What connects `C:\Users\lcolton1\AppData\Roaming\uv\tools\graphifyy\Scripts\python.exe`, `graphify`, `graphify` to the rest of the system?**
  _9 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `survey_index.py` be split into smaller, more focused modules?**
  _Cohesion score 0.14285714285714285 - nodes in this community are weakly interconnected._
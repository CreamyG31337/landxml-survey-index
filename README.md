# LandXML Survey Index

Indexes geospatial survey files in a project folder and produces a self-contained, sortable, pageable HTML report. Point it at any directory tree containing survey data and it produces a single `index.html` you can open in any browser or share with your team.

## Report tabs

- **Surfaces**: LandXML surfaces with point/face counts, version tracking, and live filter
- **Alignments**: length, start/end station in chainage format (e.g. `2+000.000`)
- **Point Clouds**: point count and density (pts/m²) read directly from LAS/LAZ headers
- **Scans**: every scan recorded in a Trimble JobXML (`.jxl`) job — scan name, job, instrument station, point count and date
- **Map**: where every file's data lies, in project coordinates. Click a point, draw an area or drop a DXF boundary to list the files with data there (see [Map](#map))
- **Orthophotos**: pixel dimensions and GSD extracted from GeoTIFF tags
- **TBC Projects**: Trimble Business Center `.vce` files with size and date

## File types indexed

| Tab | Extension | Metadata extracted |
|---|---|---|
| Surfaces | `.xml` (LandXML) | Surface name, point count, face count, version |
| Alignments | `.xml` (LandXML) | Alignment name, length, start/end station |
| Point Clouds | `.laz` / `.las` | Point count, density (pts/m²) |
| Scans | `.jxl` (Trimble JobXML) | One row per scan not marked deleted: scan name, job, instrument station (hover for coordinates), point count, date, raw scan file (hover the name) |
| Orthophotos | `.tif` / `.tiff` | Dimensions, GSD — supports classic TIFF, BigTIFF, GeoTIFF |
| TBC Projects | `.vce` | File size and date |
| CSV Files | `.csv` | Row (point) count, column count, distinct feature codes; E/N extent and elevation range |

All tabs show: date modified, filename, cloud/local availability (✓ / ☁), and a **Copy Path** button.

> **Cloud-only LandXML and JobXML:** an OneDrive online-only `.xml` or `.jxl` can't be inspected without downloading it, so by default it is left out of the Surfaces, Alignments and Scans tabs. Pass `--download-cloud-xml-mb 25` (for example) to let OneDrive download and parse cloud-only XML/JXL files up to 25 MB; the console reports how many were downloaded and how many were skipped as too large. Downloaded files stay local afterwards.

> **CSV extent:** the delimiter (`,` `;` or tab) is auto-detected. The E/N bounding box and elevation range appear as a tooltip on the **Points** cell. Coordinate columns come from a header row when one names them (`Northing`/`Easting`/`Elevation`, `X`/`Y`/`Z`, etc.). Headerless exports are placed by value size — UTM northings have 7 digits, eastings 6 — so PNEZD and PENZD both work; site-grid coordinates that can't be told apart this way are assumed to be PNEZD.

> **Footprints:** alongside the report, each run writes `index.footprints.json` (same name as the report, `.footprints.json` extension) recording where every file's data lies, for boundary and map searches. Surfaces and CSVs list the 5 m grid cells their points fall in, with a point count per cell — so a stray control point adds one isolated cell instead of stretching the extent across the site. Point clouds and orthophotos record their header bounding box only (a LAZ header's box includes any stray points).
>
> **Scans located from the job file:** large scan exports are often online-only, and reading even a LAZ header would make OneDrive download the whole file. A scanning total station's JobXML job records each scan's instrument station (with coordinates), its orientation, and the frame of directions it scanned, so the footprint is built from that instead: the station plus the frame projected onto the plan at the distance the frame was defined at (`scan_reach` in the config when not recorded). It shows where the scan was aimed rather than exactly where its points landed — expect edges to be within a few metres — and it needs only the small `.jxl`, not the point data.
>
> **Control left out of CSV footprints:** control, setups and checks would otherwise make a CSV look like it has data at every monument it touched. A CSV point is treated as control — left out of its extent and footprint, but still counted in **Points** and searchable by code — when any of these hold:
> - it lies within 10 cm of a point in the project's control list: the CSVs in the folder named by `control_under` in the [config file](#configuration). This catches control whatever each surveyor named or coded it, so it's the most reliable rule — set it if your project has a control folder.
> - its code contains a control word (`MON`, `BM41`, `CP`, `IRON BAR`, `BRASS CAP`, `CONCRETE NAIL IN`, `PK`, …), or is a bare `TEST` / `TEST-001`.
> - its point name starts with a control prefix (`CP1`, `MON3`, `Mon ABS`, `BM41`, `IB701`, …).
>
> The word and prefix lists are generic out of the box; add your crews' own codes for targets, setups and instrument checks in the config. Blank codes are never treated as control (plenty of real topo is uncoded), and neither are features that merely contain a word (`DENSITY-TEST`, `TEST-PIT-1`). Don't identify control files by name — a file called `…CONTROL.csv` may well be a full controller dump.
>
> **Find files by feature code / content:** the CSV filter box searches **file content**, not just filenames. Each CSV's distinct text values — feature codes, descriptions, layers — are indexed so that typing a code (e.g. `EOP`) narrows the list to every CSV that contains it. High-cardinality columns (point IDs, coordinates) are excluded to keep the report small. The **Codes** column additionally shows the count of distinct feature codes from the detected code column (`Code`, `Description`, `Feature`, `String`, `Layer`, … when named, otherwise the trailing description field, as in a headerless PNEZD export); hover it to see the full list.
>
> **Folder scope:** CSVs are indexed anywhere in the tree by default. To keep the tab focused on coded survey points, set `csv_under` in the [config file](#configuration) to the folder your crews file point exports in, or a list of folders (or pass `--csv-under "FRAGMENT"`, repeated for several).

## Map

The **Map** tab draws the footprints in plan, in the project's own coordinates. It works offline and sends nothing anywhere; the only imagery is your own orthophoto, if you configure one (below). Open it directly with `index.html#map`.

- **Background:** set `basemap` in the `[map]` section of the [config file](#configuration) to an orthophoto with a world file — a `.jpg` + `.jgw` (or `.png`/`.tif` + `.pgw`/`.tfw`/`.wld`), or the `.zip` a drone platform exports them in. A glob pattern picks the newest match, so pointing it at the folder where new orthos land keeps the map current. The image is cut into tiles in a folder beside the report (`index.html` → `index.basemap\`; keep the two together when copying the report) and recut only when the source changes. JPEG is recommended for big orthos: it decodes straight to the reduced scale, so a 2.4-gigapixel ortho takes seconds. Needs Pillow, which `uv run` installs automatically; without it the map just has no background. Only north-up images (no rotation terms in the world file) are supported.

- **Layers:** CSV points and surfaces are shaded by how many files have points in each grid square (darker = more data); scans show their station and the area they were aimed at; point clouds and orthophotos show their extent as a dashed outline (orthophotos are off by default — they usually cover the whole site). Alignments and the control list are drawn for reference. Unticking a layer also leaves it out of search results.
- **Search:** **Pick point** (click; set the search radius in metres), **Draw area** (click the corners; click the first corner or double-click to finish, Esc cancels), or drop a DXF onto the map / use **Load DXF boundary…** — its polylines (POLYLINE or LWPOLYLINE, ASCII DXF) become the search area.
- **Results** are grouped by type: CSV points and surfaces ranked by how many of their points fall inside (`≈` where a large surface is drawn with merged squares, so the count is approximate), scans by size, and extent-only files smallest first, since a box around the whole site says little about one building. Hover a result to highlight it on the map; **Copy path** and **Zoom to** act on it.
- **Navigation:** drag to pan, scroll to zoom, **Reset view** to fit the main site (files more than 5 km from the bulk of the data — e.g. in another coordinate system — don't affect it). Coordinates under the mouse show bottom-left.

The map embeds a compact copy of the footprints (large footprints merged into coarser squares), adding roughly 1 MB to the report on a large project.

## Requirements

- Python 3.11+ — run via `uv run` (recommended) or plain `python`
- [Everything by voidtools](https://www.voidtools.com/) *(optional but recommended)* — enables fast indexed search instead of a directory walk. Enable its HTTP server under **Tools → Options → HTTP Server**.

No third-party Python packages are needed for indexing. The map's orthophoto background uses [Pillow](https://python-pillow.org/), declared in the script's header so `uv run` installs it automatically (per user, no admin rights); with plain `python` and no Pillow, everything else still works.

## Usage

```powershell
# Index all file types → index.html written into the search directory
uv run survey_index.py --path "C:\Projects\MyProject" --batch

# LandXML surfaces only
uv run survey_index.py --path "C:\Projects\MyProject"

# Custom title and output location
uv run survey_index.py --path "C:\Projects\MyProject" --batch --title "Example Project" --output-file "D:\Reports\index.html"

# Add a logo (PNG embedded in the report)
uv run survey_index.py --path "C:\Projects\MyProject" --batch --logo "C:\logo.png"

# Sort by largest file first
uv run survey_index.py --path "C:\Projects\MyProject" --batch --sort size

# Plain text output
uv run survey_index.py --path "C:\Projects\MyProject" --output txt
```

## Configuration

Settings that depend on your project — folder layout, where the control list lives, the codes your crews use for control — go in a TOML file rather than the code. Copy `survey_index.example.toml` to `survey_index.toml` next to the script (git ignores it) and edit it; every setting is explained in the example. It's picked up automatically, or name another file with `--config FILE`. Without one, generic defaults apply. The first line of each run says which settings file was used.

| Section | Setting | Default | Purpose |
|---|---|---|---|
| `[paths]` | `priority` | `[]` | Folder fragments, most preferred first, for choosing which copy of a duplicate file to show |
| | `csv_under` | `""` | Only index CSVs under this folder fragment, or any of a list of them (`""` = anywhere) |
| | `control_under` | `""` | CSVs under this folder fragment are the control list (`""` = none) |
| `[footprints]` | `cell_size` | `5.0` | Footprint grid cell size, metres |
| | `control_tolerance` | `0.10` | How close (metres) a point must be to a control-list point to count as control |
| | `scan_reach` | `40.0` | Fallback reach (metres) for a JXL scan footprint when its frame records no distance |
| `[map]` | `basemap` | `""` | Orthophoto (+ world file, or a zip of both) to draw behind the Map tab; path or glob, newest match wins |
| | `basemap_resolution` | `0.15` | Coarsest metres per pixel for the background tiles |
| `[control]` | `code_words` | monuments, bars, caps, nails, spikes, targets, benchmarks | Words that mark a point code as control |
| | `whole_codes` | `["TEST"]` | Codes that are control only as the whole code (`TEST`, `TEST-001`) |
| | `name_prefixes` | `CP`, `MON`, `BM`, `IB`, `CTRL` | Point-name prefixes that mark control |

Command-line flags (`--csv-under`, `--control-under`, `--prefer-path`) override the file for one run.

### All options

| Flag | Default | Description |
|---|---|---|
| `--path DIR` | *(required)* | Root directory to search |
| `--config FILE` | `survey_index.toml` beside the script, if present | Settings file (see [Configuration](#configuration)) |
| `--title TEXT` | `Survey File Index` | Report title shown in the header |
| `--output-file FILE` | `<path>/index.html` | Override the HTML output path |
| `--logo FILE` | *(none)* | PNG logo to embed in the report header |
| `--everything-url URL` | `http://localhost` | Everything HTTP server URL |
| `--batch` | off | Index all file types (default: surfaces only) |
| `--mode surfaces\|alignments` | `surfaces` | Element type when not using `--batch` |
| `--sort name\|date\|size` | `name` | Initial sort order |
| `--output html\|txt` | `html` | Output format |
| `--prefer-path FRAGMENT` | *(repeatable)* | Prepend a folder fragment to the config's path-priority list |
| `--download-cloud-xml-mb MB` | `0` | Download (via OneDrive) and parse cloud-only `.xml` and `.jxl` files up to this size; larger ones are skipped. `0` skips all cloud-only XML/JXL |
| `--csv-under FRAGMENT` | config `csv_under` | *(repeatable)* Only index CSVs whose path contains this folder fragment, or any of the repeated ones (`""` = anywhere) |
| `--control-under FRAGMENT` | config `control_under` | CSVs under this folder are the project control list; points on those coordinates are left out of CSV footprints (`""` = code/name matching only) |
| `--no-hash-dedup` | off | Disable file-level hash deduplication |
| `--no-content-dedup` | off | Disable geometry-level deduplication (LandXML only) |

## Deduplication

Duplicate files are automatically collapsed:

- **Hash dedup** — files with identical content (MD5 of first 64 KB) are merged into one row showing a ×N copies count.
- **Path priority** — when choosing which copy to show, folders are preferred in the order given by `priority` in the [config file](#configuration) (the example config shows a numbered `02-DESIGN` → `03-ASBUILTS` → … layout). Add to it for one run with `--prefer-path FRAGMENT`.
- **Content dedup** (LandXML only) — surfaces/alignments with the same name and geometry counts across different files are merged. Different versions (differing point/face counts) are kept as separate rows labelled `v1/2`, `v2/2`, etc.

## Scheduled indexing

Copy `run_index.example.bat` to `run_index.bat` (which git ignores, since it holds your real paths), edit its settings, and set up `survey_index.toml` as described in [Configuration](#configuration). Then register it as a Windows scheduled task:

```bat
:: Run daily at 7am (runs as you, only when logged on - no admin needed)
schtasks /create /tn "Survey Index" /tr "C:\Projects\file-finder\run_index.bat" /sc daily /st 07:00 /f

:: Run immediately
schtasks /run /tn "Survey Index"

:: Remove
schtasks /delete /tn "Survey Index" /f
```

## Tests

`tests/test_survey_index.py` checks the parsers and footprints on small synthetic files: CSV header detection and
column guessing, control-point exclusion, LAS and GeoTIFF headers, config loading, and one end-to-end `--batch`
run. It needs no project data and no Everything service. Run with pytest:

```
uv run --with pytest python -m pytest
```

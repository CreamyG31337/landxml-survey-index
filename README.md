# Survey File Index — Strathcona Dam Upgrade

Indexes geospatial survey files in a project folder and produces a self-contained, sortable HTML report (`index.html`). Uses [Everything by voidtools](https://www.voidtools.com/) for fast file discovery and reads file headers to extract metadata without loading full files.

## Requirements

- [Everything by voidtools](https://www.voidtools.com/) running with its HTTP server enabled
  - In Everything: Tools → Options → HTTP Server → enable, default port 80
- Python via `uv run` (or any Python 3.11+, no extra packages required)

## File types indexed

| Tab | Extension | Metadata extracted |
|---|---|---|
| Surfaces | `.xml` (LandXML) | Surface name, point count, face count, version |
| Alignments | `.xml` (LandXML) | Alignment name, length |
| Point Clouds | `.laz` / `.las` | Point count, density (pts/m²) from LAS bounding box |
| Orthophotos | `.tif` / `.tiff` | Image dimensions, GSD — supports classic TIFF, BigTIFF, and GeoTIFF (ModelPixelScale, ModelTiepoint, or ModelTransformation tags) |
| TBC Projects | `.vce` | File size and date |

All tabs show: file size, date modified, filename, availability (local ✓ or cloud-only ☁), and a **Copy Path** button for pasting into TBC or Explorer.

## Output

The report is written to the project OneDrive folder so it's accessible to the whole team:

```
C:\Users\lcolton1\OneDrive - FlatironDragados\Rutherford, Chad's files - 1645 - Strathcona Dam Upgrade\index.html
```

To change the output location, edit `OUTPUT_HTML` at the top of `survey_index.py`.

## Usage

```powershell
# Index all file types → index.html
uv run survey_index.py --batch

# LandXML surfaces only
uv run survey_index.py

# Alignments only
uv run survey_index.py --mode alignments

# Sort by largest first
uv run survey_index.py --batch --sort size

# Plain text output (legacy)
uv run survey_index.py --output txt
```

## Deduplication

Duplicate files are automatically collapsed:

- **Hash dedup** — files with identical content (compared by MD5 of first 64 KB) are merged into one row showing a ×N copies count. The most canonical copy is kept based on folder priority.
- **Path priority** — when choosing the canonical copy, folders are preferred in this order: `02-DESIGN` → `03-ASBUILTS` → `05-QC SURVEY DATA` → `07-DATALOGGER BACKUP` → `01-OUTPUT` → `06-WORKING DATA\TBC` → `06-WORKING DATA`. Override with `--prefer-path FRAGMENT`.
- **Content dedup** (LandXML only) — surfaces with the same name and geometry counts across different files are merged. Genuinely different versions (differing point/face counts) are kept as separate rows labelled `v1/2`, `v2/2`, etc. Disable with `--no-content-dedup`.

## Scheduled indexing

`run_index.bat` runs `--batch` and can be registered as a Windows scheduled task:

```bat
:: Register weekly Monday 7am (run once as admin, or in HKCU via Task Scheduler GUI)
schtasks /create /tn "Survey Index" /tr "C:\Projects\file-finder\run_index.bat" /sc weekly /d MON /st 07:00 /f

:: Run immediately
schtasks /run /tn "Survey Index"

:: Remove
schtasks /delete /tn "Survey Index" /f
```

## Configure search path

Edit the `SEARCH_PATH` constant at the top of `survey_index.py`:

```python
SEARCH_PATH = r"C:\Users\you\OneDrive\Your Project Folder"
```

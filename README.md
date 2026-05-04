# file-finder

Scripts for locating specific file types by content using the [Everything](https://www.voidtools.com/) search index.

## Requirements

- [Everything by voidtools](https://www.voidtools.com/) running with its HTTP server enabled
  - In Everything: Tools → Options → HTTP Server → enable, default port 80
- Python (via `uv run` or any Python 3.10+)

## Scripts

### `find_landxml.py`

Finds LandXML files that contain `<Alignment>` elements within a specific folder.
Uses the Everything HTTP API to get a fast list of `.xml` files, then inspects each
file's XML content to confirm it's a valid LandXML with alignment data.

**Configure** the search path at the top of the script:

```python
SEARCH_PATH = r"C:\your\path\here"
```

**Run:**

```powershell
uv run python find_landxml.py
```

**Output:** prints matches to the console and writes them to `results.txt` in the same folder.

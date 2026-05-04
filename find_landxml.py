"""
Find LandXML files containing Alignment elements via the Everything HTTP API.
Requires Everything (voidtools) running with HTTP server enabled on port 80.
"""

import json
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

EVERYTHING_URL = "http://localhost"
SEARCH_PATH = r"C:\Users\lcolton1\OneDrive - FlatironDragados\Rutherford, Chad's files - 1645 - Strathcona Dam Upgrade"


def query_everything(search: str, max_results: int = 2000) -> list[dict]:
    params = urllib.parse.urlencode({
        "s": search,
        "j": "1",
        "path_column": "1",
        "count": str(max_results),
    })
    url = f"{EVERYTHING_URL}/?{params}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("results", [])


def get_alignment_names(filepath: str) -> list[str]:
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
        local_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
        if local_tag != "LandXML":
            return []
        ns = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""
        ns_prefix = f"{{{ns}}}" if ns else ""
        return [
            el.get("name", "<unnamed>")
            for el in root.iter(f"{ns_prefix}Alignment")
        ]
    except Exception:
        return []


OUTPUT_FILE = Path(__file__).parent / "results.txt"


def main():
    query = f'ext:xml path:"{SEARCH_PATH}"'
    print(f"Querying Everything for XML files in:\n  {SEARCH_PATH}\n")

    results = query_everything(query)
    print(f"Found {len(results)} XML file(s) — checking for LandXML with Alignments...\n")

    matches = []
    for item in results:
        full_path = str(Path(item["path"]) / item["name"])
        names = get_alignment_names(full_path)
        if names:
            matches.append((full_path, names))

    lines = []
    if matches:
        lines.append(f"{len(matches)} LandXML file(s) with Alignments found in:\n  {SEARCH_PATH}\n")
        for path, names in matches:
            lines.append(path)
            for name in names:
                lines.append(f"    - {name}")
        print("\n".join(lines))
    else:
        lines.append("No LandXML files with Alignments found.")
        print(lines[0])

    OUTPUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nResults written to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

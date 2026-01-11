#!/usr/bin/env python3

import json
import sys
import requests
from pathlib import Path

ROOT_URL = "https://pal.getutm.app/adp"
TIMEOUT = 30

def extract_asset_paths(obj):
    """
    Recursively walk a JSON object and yield all values of keys named 'assetPath'.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "assetPath" and isinstance(v, str):
                yield v
            else:
                yield from extract_asset_paths(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from extract_asset_paths(item)

def main(manifest_path):
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    asset_paths = sorted(set(extract_asset_paths(manifest)))
    total_assets = len(asset_paths)

    if total_assets == 0:
        print("No assetPath entries found.")
        sys.exit(1)

    print(f"Found {total_assets} unique asset paths\n")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "asset-checker/1.0"
    })

    successes = 0
    failures = 0

    for rel_path in asset_paths:
        url = f"{ROOT_URL}/{rel_path.lstrip('/')}"
        print(f"Checking: {url}")

        try:
            resp = session.head(
                url,
                allow_redirects=True,
                timeout=TIMEOUT
            )

            final_url = resp.url
            status = resp.status_code

            if status == 200:
                successes += 1
                print(f"  ✅ OK → 200 ({final_url})")
            else:
                failures += 1
                print(f"  ❌ FAILED → {status} ({final_url})")

        except requests.RequestException as e:
            failures += 1
            print(f"  ❌ ERROR → {e}")

    print("\nSummary")
    print("-------")
    print(f"Total assets: {total_assets}")
    print(f"Successful:  {successes}")
    print(f"Failed:      {failures}")

    if failures > 0:
        sys.exit(1)
    else:
        print("\nAll assets resolved successfully with HTTP 200.")
        sys.exit(0)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} manifest.json")
        sys.exit(1)

    main(Path(sys.argv[1]))

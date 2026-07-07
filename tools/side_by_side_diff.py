"""Side-by-side JSON diff between the Node dashboard (:3000) and Python port (:3001).

Both apps must be running against the same Jira credentials and the same
`data/hotfix-bookings.json` file. This script copies the Node app's bookings
store into the Python app's data folder before running so they compare apples
to apples.

Only read-only endpoints are compared. POST /book is validated separately by
the pytest suite.

Usage:
    python tools/side_by_side_diff.py
"""
from __future__ import annotations

import difflib
import json
import shutil
import sys
from pathlib import Path
from urllib.request import Request, urlopen

NODE = "http://127.0.0.1:3000"
PY = "http://127.0.0.1:3001"
BASE = "/api/hotfix-booking"

VAL_BOOKINGS = Path(r"C:\Users\ukaloan\Documents\code\val-dashboard\data\hotfix-bookings.json")
PY_BOOKINGS = Path(r"C:\Users\ukaloan\Documents\code\hotfix-booking\data\hotfix-bookings.json")

# Endpoints to compare. Some make Jira calls twice (Python does one call per
# endpoint hit), so numbers may drift by milliseconds — that's fine for JSON diff.
ENDPOINTS = [
    ("GET", "/field-options"),
    ("GET", "/deployed-cms"),
    ("GET", "/bookings"),
    ("GET", "/next-version"),
    ("GET", "/client-versions"),
    ("GET", "/history"),
    ("GET", "/history?minor=92"),
]


def fetch(base: str, path: str) -> tuple[int, object]:
    req = Request(base + BASE + path, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=60) as resp:
            body = resp.read()
            return resp.status, json.loads(body) if body else None
    except Exception as e:  # noqa: BLE001
        return -1, {"__error__": str(e)}


def normalize(obj):
    """Recursively sort dict keys and lists of primitives for stable comparison."""
    if isinstance(obj, dict):
        return {k: normalize(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        # Lists of dicts / lists are order-sensitive in the API contract, keep order.
        # Lists of primitives are sometimes returned unsorted — sort them.
        if all(not isinstance(x, (dict, list)) for x in obj):
            try:
                return sorted(obj, key=lambda x: (str(type(x)), x))
            except TypeError:
                return obj
        return [normalize(x) for x in obj]
    return obj


def compare(node_body, py_body) -> list[str]:
    a = json.dumps(normalize(node_body), indent=2, sort_keys=True).splitlines()
    b = json.dumps(normalize(py_body), indent=2, sort_keys=True).splitlines()
    if a == b:
        return []
    diff = list(difflib.unified_diff(a, b, fromfile="node", tofile="python", lineterm=""))
    # Cap to a reasonable size for output
    if len(diff) > 100:
        diff = diff[:100] + [f"... (truncated, {len(diff) - 100} more lines)"]
    return diff


def main() -> int:
    if not VAL_BOOKINGS.exists():
        print(f"ERROR: {VAL_BOOKINGS} not found — is val-dashboard checked out?")
        return 2
    PY_BOOKINGS.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(VAL_BOOKINGS, PY_BOOKINGS)
    print(f"Copied {VAL_BOOKINGS.name} -> {PY_BOOKINGS}")

    total = passed = 0
    for method, path in ENDPOINTS:
        total += 1
        print(f"\n=== {method} {BASE}{path} ===")
        node_status, node_body = fetch(NODE, path)
        py_status, py_body = fetch(PY, path)
        print(f"  node : {node_status}")
        print(f"  python: {py_status}")
        if node_status != py_status:
            print(f"  STATUS MISMATCH ({node_status} vs {py_status})")
            continue
        diff = compare(node_body, py_body)
        if not diff:
            print("  match")
            passed += 1
        else:
            print("  diff:")
            for line in diff:
                print("   ", line)

    print(f"\n{passed}/{total} endpoints match")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

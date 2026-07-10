"""Capture raw Jira responses into tests/fixtures/jira-live/.

Uses the Jira credentials from `.env` (same ones the FastAPI app uses).

Usage:
    python tools/capture_hotfix_fixtures.py
    python tools/capture_hotfix_fixtures.py --out tests/fixtures/jira-live
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

import httpx

from hotfix_booking.config import get_settings

SEARCH_FIELDS = [
    "summary",
    "status",
    "components",
    "fixVersions",
    "customfield_13235",  # Client Environments
    "customfield_10751",  # TargetDeploymentDate
    "reporter",
]

# Mirror the client-side pagination bounds so captured fixtures represent
# the full result set (Jira caps each page at ~100 issues).
_MAX_PAGES = 15
_PAGE_SIZE = 100

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "jira-live"


def auth_headers(email: str, token: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def search_jql(client: httpx.Client, jql: str) -> dict:
    """Paginate through /search/jql and return a synthesised response
    containing every issue. Top-level `isLast` is forced to true; the
    tail `nextPageToken` (if any) is dropped so downstream tests can
    treat the fixture as terminal without hitting live Jira again."""
    all_issues: list[dict] = []
    next_page_token: str | None = None
    for _ in range(_MAX_PAGES):
        payload: dict[str, object] = {
            "jql": jql,
            "fields": SEARCH_FIELDS,
            "maxResults": _PAGE_SIZE,
        }
        if next_page_token is not None:
            payload["nextPageToken"] = next_page_token
        resp = client.post("/rest/api/3/search/jql", json=payload)
        resp.raise_for_status()
        body = resp.json() or {}
        all_issues.extend(body.get("issues") or [])
        if body.get("isLast", True):
            break
        next_page_token = body.get("nextPageToken")
        if not next_page_token:
            break
    else:
        print(
            f"  WARN: pagination cap ({_MAX_PAGES} pages) hit — fixture may be truncated."
        )
    return {"issues": all_issues, "isLast": True}


def write(out_dir: Path, name: str, data: object) -> None:
    p = out_dir / name
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  wrote {p}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output directory")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.jira_base_url or not settings.jira_api_token:
        print("ERROR: JIRA_BASE_URL / JIRA_API_TOKEN not set. Populate .env first.", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(
        base_url=settings.jira_base_url,
        headers=auth_headers(settings.jira_email, settings.jira_api_token),
        timeout=60.0,
    ) as client:
        print("Fetching CM components...")
        r = client.get("/rest/api/3/project/CM/components")
        r.raise_for_status()
        write(out_dir, "cm_components.json", r.json())

        print("Fetching client-environment options...")
        r = client.get(
            f"/rest/api/3/field/customfield_13235/context/{settings.client_context_id}/option"
        )
        r.raise_for_status()
        write(out_dir, "client_options.json", r.json())

        print("Fetching deployed CMs (last 120d)...")
        deployed = search_jql(
            client,
            'project = CM AND status in ("Deployment Completed", "Done") '
            "AND created >= -120d ORDER BY created DESC",
        )
        write(out_dir, "search_deployed.json", deployed)

        print("Fetching all CMs (last 120d)...")
        all_cms = search_jql(
            client,
            "project = CM AND created >= -120d ORDER BY created DESC",
        )
        write(out_dir, "search_all.json", all_cms)

        # Pick a real major.minor from the deployed data for the by-version query.
        semver = re.compile(r"^(\d+)\.(\d+)\.\d+$")
        seen: set[tuple[int, int]] = set()
        for issue in deployed.get("issues", []) or []:
            for v in issue.get("fields", {}).get("fixVersions", []) or []:
                m = semver.match(v.get("name", ""))
                if m:
                    seen.add((int(m.group(1)), int(m.group(2))))
        if not seen:
            print("WARN: no semver fixVersions found; skipping by-version fixture.")
            return 0
        major, minor = sorted(seen, reverse=True)[0]
        print(f"Fetching CMs for {major}.{minor}.*...")
        by_ver = search_jql(
            client,
            f'project = CM AND fixVersion ~ "{major}.{minor}.*" ORDER BY created DESC',
        )
        write(out_dir, f"search_by_version_{major}_{minor}.json", by_ver)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

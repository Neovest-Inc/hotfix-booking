"""Capture raw Jira responses into tests/fixtures/jira-live/.

This is the Python-native replacement for the old Node capture script.
Uses the same Jira credentials the FastAPI app uses (from .env), so the
`hotfix-booking` project has no dependency on the val-dashboard repo.

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
    resp = client.post(
        "/rest/api/3/search/jql",
        json={"jql": jql, "fields": SEARCH_FIELDS, "maxResults": 500},
    )
    resp.raise_for_status()
    return resp.json()


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

        print("Fetching deployed CMs (last 100d)...")
        deployed = search_jql(
            client,
            'project = CM AND status in ("Deployment Completed", "Done") '
            "AND created >= -100d ORDER BY created DESC",
        )
        write(out_dir, "search_deployed.json", deployed)

        print("Fetching all CMs (last 100d)...")
        all_cms = search_jql(
            client,
            "project = CM AND created >= -100d ORDER BY created DESC",
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

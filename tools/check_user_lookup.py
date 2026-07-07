"""Quick sanity check that Jira's user-search API can resolve emails to
displayNames. Uses the credentials from .env.

Usage:
    python tools/check_user_lookup.py [email ...]

Examples:
    python tools/check_user_lookup.py iqueiroz@neovest.com nzhang@neovest.com
"""
from __future__ import annotations

import sys

import httpx

from hotfix_booking.config import get_settings
from hotfix_booking.jira_client import _auth_header


def main() -> int:
    emails = sys.argv[1:] or ["iqueiroz@neovest.com", "nzhang@neovest.com"]
    settings = get_settings()
    if not settings.jira_base_url:
        print("ERROR: JIRA_BASE_URL not set. Populate .env first.", file=sys.stderr)
        return 1

    with httpx.Client(
        base_url=settings.jira_base_url,
        headers=_auth_header(settings),
        timeout=30.0,
    ) as client:
        for email in emails:
            r = client.get("/rest/api/3/user/search", params={"query": email})
            print(f"--- {email} ---")
            print(f"  status: {r.status_code}")
            if r.status_code != 200:
                print(f"  body:   {r.text[:400]}")
                continue
            users = r.json()
            if not users:
                print("  (no matches)")
                continue
            for u in users:
                print(
                    f"  displayName: {u.get('displayName')} | "
                    f"accountId:   {u.get('accountId')} | "
                    f"active:      {u.get('active')} | "
                    f"emailAddress: {u.get('emailAddress')}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())

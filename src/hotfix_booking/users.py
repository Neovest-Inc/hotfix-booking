"""User lookup helpers.

`resolve_jira_user(email, users)` picks the "real" Jira user record for an email
from the raw list Jira's user-search returns. Jira often returns extra service
accounts (accountId prefixed `qm:`, empty emailAddress) — those get filtered out.
"""
from __future__ import annotations

from typing import Optional


def resolve_jira_user(email: str, users: list[dict]) -> Optional[dict]:
    """Return the best matching real user record, or None if no clean match.

    Selection rules:
      1. Must be `active` (not disabled).
      2. `emailAddress` must equal the query email (case-insensitive).
    """
    if not email:
        return None
    target = email.strip().lower()
    for u in users:
        if not u.get("active"):
            continue
        ea = (u.get("emailAddress") or "").strip().lower()
        if ea and ea == target:
            return u
    return None

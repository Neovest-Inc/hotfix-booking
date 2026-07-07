"""Semantic version parsing/comparison — mirrors server/hotfix-booking.js exactly.

Node reference:
    function parseVersion(version) {
      const parts = version.split('.').map(Number);
      return { major: parts[0] || 0, minor: parts[1] || 0, patch: parts[2] || 0 };
    }
    function compareVersions(a, b) {
      const vA = parseVersion(a); const vB = parseVersion(b);
      if (vA.major !== vB.major) return vA.major - vB.major;
      if (vA.minor !== vB.minor) return vA.minor - vB.minor;
      return vA.patch - vB.patch;
    }
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int


def parse_version(version: str) -> Version:
    """Parse "9.92.76" → Version(9,92,76). Missing/invalid parts default to 0.

    Matches JS `parts[i] || 0`: `Number("")` is NaN (→ 0), `Number("abc")` is NaN (→ 0),
    `Number("07")` is 7. An entirely empty string → (0,0,0).
    """
    parts = (version or "").split(".")

    def to_int(s: str) -> int:
        if s == "":
            return 0
        try:
            n = int(s, 10)
        except ValueError:
            return 0
        return n or 0  # JS `|| 0` treats 0 as falsy — kept 0 either way

    major = to_int(parts[0]) if len(parts) > 0 else 0
    minor = to_int(parts[1]) if len(parts) > 1 else 0
    patch = to_int(parts[2]) if len(parts) > 2 else 0
    return Version(major, minor, patch)


def compare_versions(a: str, b: str) -> int:
    """Return positive if a>b, negative if a<b, 0 if equal. Sign only (magnitude not asserted)."""
    va = parse_version(a)
    vb = parse_version(b)
    if va.major != vb.major:
        return va.major - vb.major
    if va.minor != vb.minor:
        return va.minor - vb.minor
    return va.patch - vb.patch


def is_semver(s: str) -> bool:
    """Matches Node regex `/^\\d+\\.\\d+\\.\\d+$/`."""
    return bool(s) and bool(_SEMVER_RE.match(s))

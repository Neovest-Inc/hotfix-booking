# Plan: Maintenance-release CMs — Confluence-cached component broadcast

> **Status: PAUSED** — designed but not yet implemented. Pick this back up when ready.

## TL;DR

Maintenance-release CMs (versions with `patch == 0`, e.g. `9.96.0`) in Jira have `components: []` because they upgrade the app as a coordinated bundle. Our matrix skips them, so `9.96.0` never appears — e.g. CL005 × Configuration still shows `9.95.89` even though the client is actually running `9.96.0`. Fix: authoritative source is the Confluence "X.Y.0 - Maintenance Release" page in space LAYERONE, which lists **Components** (included) and **Service Name** (excluded) tables. Refresh the cache daily via an in-app asyncio background task, store as `data/maintenance-releases.json`. Matrix consults the cache when it sees a `.0` CM with empty components: broadcast to included components only, for the CM's wave clients only. Excluded components keep their last per-component version, so hotfix numbering stays correct.

## Design

### Source of truth: Confluence

- Page URL pattern: `https://neovest.atlassian.net/wiki/spaces/LAYERONE/pages/<id>/<X.Y.0>+-+Maintenance+Release`
- Fetch via Confluence Cloud REST API v2: `GET /wiki/api/v2/pages/<id>?body-format=atlas_doc_format` (ADF tree, easier to walk than HTML).
- Discovery: search by title in the LAYERONE space with `GET /wiki/api/v2/spaces/{key}/pages?title=<X.Y.0 - Maintenance Release>` (or CQL fallback: `space = LAYERONE AND title = "<X.Y.0> - Maintenance Release"`).
- Auth: same Basic auth as Jira (email + API token). Same base host (`neovest.atlassian.net`).

### Page structure (confirmed via user screenshots)

- Heading "Components for the release - Staging Report", followed by a subheading paragraph "Release branch: release/X.Y.0".
- **Table with header cell text = "Components"** → single column, each row is an included component (e.g. `Analytics`, `BBGRisk`, `CorporateActions`, `Configuration`, ...).
- Heading "Components not included:".
- **Table with header cell text = "Service Name"** → single column, each row is an excluded component (e.g. `AExeo`, `Alerts`, `BBG SAPI`, ...).

### Parser strategy

- Walk the ADF `content` array. Extract every table node.
- For each table, read the first row's first cell text. If it matches "Components" (case-insensitive, whitespace-stripped) → included. If "Service Name" → excluded.
- For each subsequent row, extract cell 0's text content (join all text nodes).
- Ignore rows with empty text (blank rows).
- If the "Components" table isn't found, log a warning and skip that release (cache retains whatever's already there).

### Name normalization

Confluence writes `CorporateActions` (no space); Jira writes `Corporate Actions` (with space). Component name comparison uses a normalized form: `re.sub(r"\s+", "", name).lower()`. Store both raw and normalized names in the cache so we can debug mismatches.

### Cache file: `data/maintenance-releases.json`

```json
{
  "9.96.0": {
    "included": ["Analytics", "BBGRisk", "CorporateActions", "Configuration", "..."],
    "includedNormalized": ["analytics", "bbgrisk", "corporateactions", "configuration", "..."],
    "excluded": ["AExeo", "Alerts", "..."],
    "excludedNormalized": ["aexeo", "alerts", "..."],
    "pageId": "5810782209",
    "pageUrl": "https://neovest.atlassian.net/wiki/spaces/LAYERONE/pages/5810782209/9.96.0+-+Maintenance+Release",
    "fetchedAt": "2026-07-10T14:00:00Z"
  },
  "9.97.0": { "...": "..." }
}
```

Git-ignored (like `hotfix-bookings.json`).

### Refresh trigger: in-app daily background task

- FastAPI startup (`app.py` lifespan) launches a single asyncio task.
- Task loop:
  1. If cache is missing OR the newest `fetchedAt` is >24h old → refresh now.
  2. Otherwise → `await asyncio.sleep(seconds_until_next_daily_tick)`.
  3. Repeat.
- Refresh:
  - Determine which `.0` versions to look up: the set of maintenance-release versions currently present in the last matrix fetch (or a simpler heuristic — every `.0` version that appears in any CM's fixVersions from the last 120-day window).
  - For each, look up its page in Confluence, parse, update the cache entry. Never remove existing entries (historic releases stay in the cache forever — cheap, useful for archived data).
  - Atomic write: write to a temp file, rename to final path.
- Errors during a refresh:
  - Individual page failure (page missing / parse failed) → log warning, keep previous cache entry.
  - Whole-refresh failure (Confluence down, auth error) → log error, retry on next tick. Previous cache stays in place.

### Matrix consumption

- On startup, `store.py` (or a new `maintenance_releases.py`) loads `data/maintenance-releases.json` once and exposes `get_maintenance_release(version) -> {included_normalized, excluded_normalized} | None`.
- In `matrix.py`, when a CM has empty `components` AND `is_maintenance_release(version)`:
  - Look up the version in the cache.
  - **Found** → broadcast to intersection of (included components, components seen elsewhere in the fetched CM data). This keeps the matrix's column universe grounded — we don't invent columns for included components no one else has activity on.
  - **Not found** (page missing, cache stale, parser failed) → skip as today (safe fallback).
- Broadcast target excludes components in the `excludedNormalized` set (defensive redundancy — an excluded component shouldn't be in the included set, but if the page is malformed we still respect exclusions).

## Steps

### Phase 1 — Version helper + cache module

1. Add `is_maintenance_release(version: str) -> bool` to `src/hotfix_booking/versioning.py`. True when `is_semver(version)` and `parse_version(version).patch == 0`.
2. Add `normalize_component_name(name: str) -> str` to `src/hotfix_booking/matrix.py` (or a shared util). `re.sub(r"\s+", "", name).lower()`.
3. Add `src/hotfix_booking/maintenance_releases.py`:
   - `load_cache(path) -> dict[str, MaintenanceReleaseEntry]`
   - `save_cache(path, entries)` (atomic write)
   - `MaintenanceReleaseEntry` dataclass or TypedDict.
4. Unit tests for both (`tests/test_versioning.py`, `tests/test_maintenance_releases.py`).

### Phase 2 — Confluence client + ADF parser

5. Add `ConfluenceClient` in `src/hotfix_booking/jira_client.py` (or new `confluence_client.py` if it grows). Methods:
   - `find_release_page(version: str) -> {id, title, url} | None` — CQL/title search.
   - `fetch_page_adf(page_id: str) -> dict` — raw ADF JSON.
6. Add `parse_release_page(adf: dict) -> {included: list[str], excluded: list[str]}` — walks ADF tables, matches by header cell text.
7. Unit tests with synthetic ADF fixtures (`tests/fixtures/confluence/9_96_0_maintenance_release.json`) covering: header cell match, empty rows filtered, missing "Components" table.

### Phase 3 — Refresh orchestrator + config

8. Add `refresh_maintenance_releases(cms, cache_path, confluence)` in `maintenance_releases.py`:
   - Extract candidate `.0` versions from the CM list's `fixVersions`.
   - For each, fetch + parse + update cache entry.
   - Return summary (refreshed, skipped, errors) for logging.
9. Add config setting `MAINTENANCE_RELEASES_FILE` (default `data/maintenance-releases.json`) to `config.py`.
10. Unit tests with respx-mocked Confluence responses.

### Phase 4 — In-app background task

11. In `src/hotfix_booking/app.py`, add a FastAPI lifespan handler that:
    - On startup: launches `asyncio.create_task(refresh_loop())`.
    - On shutdown: cancels the task, awaits with timeout.
12. Add `refresh_loop()`:
    - Immediate refresh if cache empty / stale (>24h).
    - Then `await asyncio.sleep(86400)` and repeat.
    - Wraps refresh in `try/except Exception` — logs and continues.
13. Test the lifespan wiring by unit-testing the loop logic separately (not the full FastAPI lifespan, which is finicky in tests).

### Phase 5 — Matrix consumption

14. Modify `build_version_matrix` in `matrix.py`:
    - Accept an optional `maintenance_cache: dict[str, MaintenanceReleaseEntry] | None = None` parameter.
    - Pre-scan CMs to build the `known_components` universe (union of non-empty `components` across all input CMs, normalized).
    - When processing a CM with empty `components` and `is_maintenance_release(version)`:
      - If `maintenance_cache` has the version → broadcast to `included ∩ known_components`, minus `excluded`.
      - If not in cache → skip as today.
15. Update `/client-versions` route in `routes.py` to load the cache once and pass it in.
16. Full test coverage in `tests/test_matrix.py` — see Phase 6 test list below.

### Phase 6 — Tests

17. `tests/test_matrix.py` — new `TestMaintenanceReleaseBroadcast`:
    - `test_broadcasts_to_included_components_only` — cache has included=[A, B], excluded=[C]. CM has empty components, clients=[CL005]. Feed a hotfix CM elsewhere with components=[A, B, C, D]. Assert CL005 receives the maintenance version on A, B (in cache included ∩ known_components) but not on C or D.
    - `test_no_cache_entry_skips_maintenance_cm` — CM is `.0` with empty components, but version not in cache. Assert no cells populated (safe fallback).
    - `test_hotfix_with_empty_components_still_skipped` — CM with `patch != 0` and empty components → skipped regardless of cache.
    - `test_deployed_and_inflight_buckets_both_broadcast` — one `.0` CM in `Deployment Completed`, another in `Open`. Assert first is the headline, second is in the chip.
    - `test_client_scope_only_wave_clients` — maintenance CM lists [CL005]. Assert CL009 (present via other CMs) does NOT receive the maintenance version.
    - `test_excluded_component_never_broadcast_even_if_in_included` — defensive: if page has A in both included and excluded (malformed), A is not broadcast.
18. `tests/test_maintenance_releases.py` — parser, cache load/save, atomic write, corrupt file handling, refresh orchestrator with respx-mocked Confluence.
19. `tests/test_versioning.py` — `is_maintenance_release` cases.
20. Full suite: `pytest` — green.

### Phase 7 — Docs & scaffolding

21. Add `data/.gitkeep` if not present (already there per structure).
22. Update `.gitignore` to include `data/maintenance-releases.json` (mirror `hotfix-bookings.json` treatment).
23. Update `CLAUDE.md`:
    - Add `MAINTENANCE_RELEASES_FILE` to the config settings list.
    - Add a "Maintenance releases" subsection under "Known behaviors" describing the Confluence source, daily refresh, cache fallback.
    - Note the `data/maintenance-releases.json` file in the project layout.

## Relevant files

- `src/hotfix_booking/versioning.py` — `is_maintenance_release`.
- `src/hotfix_booking/maintenance_releases.py` (new) — cache load/save, refresh orchestrator, name normalization.
- `src/hotfix_booking/jira_client.py` or `confluence_client.py` (new) — Confluence page search + ADF fetch.
- `src/hotfix_booking/matrix.py` — accept cache, two-pass build, broadcast + exclusion logic.
- `src/hotfix_booking/app.py` — lifespan handler with asyncio background refresh task.
- `src/hotfix_booking/routes.py` — pass cache into `build_version_matrix`.
- `src/hotfix_booking/config.py` — `MAINTENANCE_RELEASES_FILE` setting.
- `tests/test_versioning.py`, `tests/test_matrix.py`, `tests/test_maintenance_releases.py` (new) — coverage.
- `tests/fixtures/confluence/` (new) — synthetic ADF fixtures.
- `data/maintenance-releases.json` — cache file (git-ignored).
- `.gitignore`, `CLAUDE.md` — housekeeping.

## Verification

1. `pytest` — full suite green (262+ existing tests plus new ones).
2. `pytest tests/test_maintenance_releases.py tests/test_matrix.py -v` — new tests green.
3. Manual smoke test: start uvicorn, watch logs for "Refreshing maintenance-release cache..." on startup. After ~30s, check `data/maintenance-releases.json` — should have `9.96.0` (and probably `9.97.0`) entries.
4. Manual UI check: CL005 × Configuration → `9.96.0`. CL005 × Alerts → NOT `9.96.0` (Alerts is excluded per screenshot). CL009 × Configuration → still `9.95.xx` (CL009 not on the 9.96.0 wave).
5. Failure mode check: rename `data/maintenance-releases.json` briefly, refresh matrix — should silently fall back to today's behavior (empty-components CMs skipped). Rename back → normal behavior resumes.

## Decisions

- **Confluence as source of truth** for included/excluded components. Not inferred from CM structure or JQL heuristics.
- **In-app asyncio background task** for refresh, not external Task Scheduler. Runs daily (`sleep(86400)`) plus immediate refresh on startup if cache is stale/missing.
- **Cache file, git-ignored.** Local per environment; refresh regenerates it. No manual editing expected.
- **Name normalization = strip whitespace + lowercase.** Handles the `CorporateActions` ↔ `Corporate Actions` gap.
- **Broadcast intersects with `known_components`** (components seen elsewhere in the CM data). Keeps the matrix column universe grounded — no phantom columns.
- **`excluded` list is defensively subtracted** even from the `included` intersection. Extra safety against a malformed page.
- **Historic cache entries kept forever.** Refresh is add-only; never removes an entry. Cheap and useful for looking back.
- **Individual page failures are non-fatal.** Log a warning, keep the previous entry (or leave the release out until it works). App keeps running.

## Out of scope

- Manual "refresh now" HTTP endpoint (not needed — restart or wait 24h).
- UI indicator for "this cell is a broadcast maintenance value" vs "explicit hotfix". Cell just shows the version.
- Backfilling historical cache entries for releases that no longer appear in the 120-day CM window. If it's not in the CMs, we don't ask Confluence.
- Handling `.0` CMs that have explicit components (currently unseen). Would flow through unchanged — the maintenance-broadcast path only triggers on empty components.
- Confluence write-back or automation of the page itself.

## Context for picking this up later

- **Problem discovered:** CL005 × Configuration shows `9.95.89` in our tool, but the client is actually running `9.96.0`. Investigation traced to four `.0` "wave" CMs (CM-11055, CM-11056, CM-11315, CM-11408) all with `components: []`. Our matrix skips CMs with empty components, so `9.96.0` never appears anywhere.
- **The "other tool"** that shows `9.96.0` is DevOps-maintained, reads `version.txt` off each client's server, and presumably only counts `Done` status (which is why it showed `9.94.125` for CL009 × Corporate Actions in a separate investigation instead of our correct `9.94.127`). Neither tool is wholly authoritative on its own — ours is more accurate for per-component hotfixes, theirs is more accurate for maintenance-release rollups.
- **Related prior work:** Pagination + 120-day window fix landed just before this plan was paused (`_search_jql` now paginates via `nextPageToken`, cap 15 pages / 1500 CMs; window bumped from `-100d` to `-120d`). See git log for details.

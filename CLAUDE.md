# Instructions for coding agents

You are working on `hotfix-booking` — a standalone Python (FastAPI) service that helps coordinate hotfix versions across clients and components using data from Jira. Follow these rules unconditionally unless the user explicitly says otherwise.

## Golden rules

1. **Test-Driven Development is mandatory.** For any behavior change:
   - Write the failing test first
   - Run `pytest tests/test_<name>.py -v` — confirm it fails for the expected reason
   - Implement the change
   - Run the same test — confirm it passes
   - Run `pytest` — confirm the full suite is still green
   - Never commit unless the full suite passes

2. **Ask before destructive or hard-to-reverse actions.** Rewriting git history, force-pushing, deleting files that aren't part of the current task, changing external interfaces, adding scheduled tasks — always confirm first.

3. **Do not add dependencies without approval.** Current runtime: `fastapi`, `uvicorn`, `httpx`, `python-dotenv`. Dev: `pytest`, `pytest-asyncio`, `respx`. New packages need a real justification.

4. **Do not reformat unrelated code, add speculative comments, or add docstrings to code you didn't change.** Follow the existing style.

5. **The user is often a Product Manager, not a developer.** Explain in plain language, avoid jargon, and translate technical trade-offs into business terms.

## Tech stack

- Python 3.11+
- FastAPI + uvicorn (web framework + server)
- httpx (Jira HTTP client, also used by tests as the ASGI transport)
- pytest + respx (tests + HTTP mocking)
- Vanilla JS/HTML/CSS front-end (no build step, no framework)

## Project layout

```
src/hotfix_booking/
  app.py            FastAPI factory, /health, NoCacheStaticFiles mount
  config.py         Settings loaded from .env (JIRA_*, BOOKINGS_FILE,
                    CLIENT_CONTEXT_ID, PORT, BOOKING_RETENTION_DAYS)
  jira_client.py    httpx wrapper for the 3 Jira endpoint families we use
  versioning.py     parse_version / compare_versions / is_semver
  store.py          load/save/create bookings, threading-lock, MalformedBookingsError
  matrix.py         build_version_matrix
  history.py        merge_hotfixes / derive_minor_versions (cross-major, top 8) /
                    calculate_next_version (optional major/minor filter) /
                    cleanup_bookings (deploy- + age-based)
  routes.py         The 7 HTTP endpoints under /api/hotfix-booking
  models.py         Pydantic request/response models
static/             HTML/CSS/JS served to the browser (no-cache headers)
tests/
  fixtures/jira/       Hand-crafted synthetic Jira responses (hermetic tests)
  fixtures/jira-live/  Real Jira responses (git-ignored; local only)
  test_*.py            One file per module + per endpoint group
  conftest.py          Shared fixtures: bookings_file, settings, client, mock_jira
tools/
  capture_hotfix_fixtures.py   Refreshes tests/fixtures/jira-live/
  smoke.py                     Quick sanity check without full test run
```

## Running things

```powershell
# One-time setup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]

# Start the app (auto-reloads on file change)
uvicorn hotfix_booking.app:app --reload --port 3001

# Tests
pytest                                   # all tests
pytest tests/test_versioning.py -v       # one file, verbose
pytest -k "not live_fixtures"            # skip the canary
python tools\capture_hotfix_fixtures.py  # refresh canary against real Jira
```

## HTTP endpoints

All under `/api/hotfix-booking`:

| Method | Path | Purpose |
|---|---|---|
| GET  | `/field-options`   | Components + client envs (from Jira) |
| GET  | `/deployed-cms`    | All CMs from the last 100 days |
| GET  | `/next-version` `?major=&minor=` | Next available hotfix version for the given release line (or current if omitted). Response also includes `currentMajor`, `currentMinor`, and up to 8 `minorVersions` for the release dropdown. **Auto-cleanup side effect** (deploy- + age-based). Uses `deployed_only=False` so newly-opened release lines with no deploys yet (e.g. brand-new 9.98.x) appear in the dropdown. |
| GET  | `/bookings` `?major=&minor=` | Local bookings, optionally filtered to a release line |
| POST | `/book`            | Create a booking. Validates semver, checks fresh next-version against Jira for the submitted `version`'s release line. Returns 400 on invalid input, 409 with `currentNext` if the version is no longer the next available, 500 on Jira failure. |
| GET  | `/client-versions` | Client × component version matrix |
| GET  | `/history` `?major=&minor=` | Merged deployed + booked list for a given release line. Response includes `currentMajor`, `currentMinor`, `targetMajor`, `targetMinor`, `minorVersions`. |

Release-line dropdowns auto-discover the top 8 `(major, minor)` pairs across all majors currently active in Jira — so when `10.0.x` starts appearing in real data it shows up automatically alongside `9.99.x`.

## Current known behaviors — ask before changing

These are the app's current behaviors. Some are limitations, some are deliberate. If you spot one and think it should change, propose it and wait for approval — do not fix silently.

- No authentication on any endpoint
- `bookedBy` is hard-coded to `"Dashboard User"` by the front-end
- Booking auto-cleanup runs as a side effect of `GET /next-version` (both deploy-based and age-based, threshold `BOOKING_RETENTION_DAYS`, default 180)
- Concurrency: writes are serialized by a `threading.Lock` in `store.py`, which is safe for a single uvicorn worker. Multi-worker deployments would need an OS-level file lock (`filelock` package) — swap the `bookings_lock()` implementation in `store.py`
- All user-facing timestamps in the UI are shown in Eastern Time (`America/New_York`) and suffixed with " ET"

## Common pitfalls

- **Do not overwrite `tests/fixtures/jira/` with real Jira data.** Those files are hand-crafted synthetic fixtures, and the hermetic tests assert exact values from them. Real Jira data belongs in `tests/fixtures/jira-live/`.
- **When testing endpoint behavior that depends on Jira's JQL filter** (e.g. `status in ("Deployment Completed", "Done")`), simulate the filter in your respx `side_effect` — don't blindly return the same fixture for every JQL. Otherwise you'll fake-pass tests that would fail against real Jira. See `test_dropdown_includes_lines_with_only_in_progress_cms` for the pattern.
- **Run `node --check static/hotfix-booking.js` after every JS edit.** JS has no compile step and browsers may silently drop a whole IIFE on a syntax error — the pytest suite won't catch this. It burned us once already.
- **When tests fail, read them.** Test names are descriptive and reveal the intended behavior.
- **`data/hotfix-bookings.json` is git-ignored** (per `.gitignore` — `data/*` except `.gitkeep`). Don't commit it.
- **Static assets have cache-busting `?v=N` query strings** in `static/index.html`. Bump the number when making a coordinated JS+HTML change that could otherwise break with stale-JS/new-HTML. `NoCacheStaticFiles` in `app.py` also sends `Cache-Control: no-store` headers as a belt-and-suspenders measure.

## Style

- Concise responses; no emojis unless the user asks
- Verify state (`git status`, file contents, test output) before making changes
- End of turn: one short summary of what changed. No recap lists or "I also did..." tails.
- When the user asks a direct question, answer it directly first, then act

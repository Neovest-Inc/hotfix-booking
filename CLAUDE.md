# Instructions for coding agents

You are working on `hotfix-booking` — a standalone Python (FastAPI) service that replaced a Node/Express feature from the `val-dashboard` project. Follow these rules unconditionally unless the user explicitly says otherwise.

## Golden rules

1. **Test-Driven Development is mandatory.** For any behavior change:
   - Write the failing test first
   - Run `pytest tests/test_<name>.py -v` — confirm it fails for the expected reason
   - Implement the change
   - Run the same test — confirm it passes
   - Run `pytest` — confirm the full suite is still green
   - Never commit unless the full suite passes

2. **Strict behavioral parity with the Node original.** This app was ported from `val-dashboard` (Node/Express) to match its behavior exactly, including known quirks (listed below). Do not "improve" behavior without explicit user approval. If you notice something that looks wrong, flag it and wait — do not fix.

3. **Ask before destructive or hard-to-reverse actions.** Rewriting git history, force-pushing, deleting files that aren't part of the current task, changing external interfaces, adding scheduled tasks — always confirm first.

4. **Do not add dependencies without approval.** Current runtime: `fastapi`, `uvicorn`, `httpx`, `python-dotenv`. Dev: `pytest`, `pytest-asyncio`, `respx`. New packages need a real justification.

5. **Do not reformat unrelated code, add speculative comments, or add docstrings to code you didn't change.** Follow the existing style.

6. **The user is often a Product Manager, not a developer.** Explain in plain language, avoid jargon, and translate technical trade-offs into business terms.

## Tech stack

- Python 3.11+
- FastAPI + uvicorn (web framework + server)
- httpx (Jira HTTP client, also used by tests as the ASGI transport)
- pytest + respx (tests + HTTP mocking)
- Vanilla JS/HTML/CSS front-end (copied verbatim from the Node original — do not rewrite)

## Project layout

```
src/hotfix_booking/
  app.py            FastAPI factory, /health, static mount
  config.py         Settings loaded from .env
  jira_client.py    httpx wrapper for the 3 Jira endpoint families we use
  versioning.py     parse_version / compare_versions / is_semver
  store.py          load/save/create bookings (JSON file)
  matrix.py         build_version_matrix
  history.py        merge_hotfixes / derive_minor_versions / calculate_next_version
  routes.py         The 7 HTTP endpoints under /api/hotfix-booking
  models.py         Pydantic request/response models
static/             HTML/CSS/JS served to the browser
tests/
  fixtures/jira/       Hand-crafted synthetic Jira responses (hermetic tests)
  fixtures/jira-live/  Real Jira responses (git-ignored; local only)
  test_*.py            One file per module + per endpoint group
  conftest.py          Shared fixtures: bookings_file, settings, client, mock_jira
tools/
  capture_hotfix_fixtures.py   Refreshes tests/fixtures/jira-live/
  side_by_side_diff.py         Diffs this app's JSON against the Node original
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
| GET  | `/next-version`    | Next available hotfix version. **Auto-cleanup side effect.** |
| GET  | `/bookings`        | Contents of the local bookings file |
| POST | `/book`            | Create a booking (validation + duplicate detection) |
| GET  | `/client-versions` | Client × component version matrix |
| GET  | `/history`         | Merged deployed + booked list for a given minor version |

## Intentionally-preserved quirks (do NOT "fix" without approval)

These mirror the Node original.

- No authentication on any endpoint
- `bookedBy` is hard-coded to `"Dashboard User"` by the front-end
- No file lock on the bookings JSON — concurrent POSTs could theoretically lose data
- Auto-cleanup of stale bookings runs as a side effect of `GET /next-version`

## Common pitfalls

- **Do not overwrite `tests/fixtures/jira/` with real Jira data.** Those files are hand-crafted synthetic fixtures, and the hermetic tests assert exact values from them. Real Jira data belongs in `tests/fixtures/jira-live/`.
- **The front-end is a verbatim copy** from the Node predecessor. Do not modernize (React, bundlers, framework migration, etc.) without a very good reason and explicit approval.
- **Timestamps** are Python-native ISO 8601 with microsecond precision. This deliberately differs from the Node original's millisecond format. Documented and accepted — do not "fix".
- **When tests fail, read them.** Test names are descriptive and reveal the intended behavior.
- **`data/hotfix-bookings.json` is git-ignored** (per `.gitignore` — `data/*` except `.gitkeep`). Don't commit it.

## Style

- Concise responses; no emojis unless the user asks
- Verify state (`git status`, file contents, test output) before making changes
- End of turn: one short summary of what changed. No recap lists or "I also did..." tails.
- When the user asks a direct question, answer it directly first, then act

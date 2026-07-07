# hotfix-booking

Python (FastAPI) port of the HotFix Booking feature from the `val-dashboard` Node app.
Behavioral parity is intentional — see [/memories/session/plan.md](../.copilot/dummy) for the design notes.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
copy .env.example .env   # then edit .env with real Jira creds
uvicorn hotfix_booking.app:app --reload --port 3001
```

Open http://localhost:3001/ for the UI.

## Tests

The suite has 99 automated tests split into two groups:

**Hermetic tests (91)** — Jira is mocked with `respx` against small synthetic fixtures
in `tests/fixtures/jira/`. Fast, deterministic, no network. Always run.

**Live-fixture tests (8)** — parse real Jira responses (from `tests/fixtures/jira-live/`)
through the code path. They act as a canary for Jira API drift. Self-skip if the
`jira-live/` folder is absent.

### Run everything

```powershell
pytest
```

### Refresh the live-fixture canary and re-run

The live fixtures go stale — Jira changes as CMs are raised/deployed. Re-capture
them from the real Jira, then re-run the suite:

```powershell
# 1. Recapture fresh Jira responses (needs val-dashboard next to this repo,
#    and val-dashboard/.env populated with Jira creds).
cd ..\val-dashboard
node tools\capture-hotfix-fixtures.js
cd ..\hotfix-booking

# 2. Run all tests
pytest
```

Recommended cadence: before every release, and any time you suspect Jira has
changed something. Once the app is deployed to a shared server, wire this into
a nightly job on that server so drift is caught within 24 hours.

### Run just one group

```powershell
pytest tests/test_live_fixtures.py     # canary only
pytest -k "not live_fixtures"          # everything except the canary
pytest tests/test_api_bookings.py -v   # a specific file, verbose output
```

### While developing a new feature (TDD loop)

1. Write the test first — expect it to fail: `pytest tests/test_<name>.py -v`
2. Implement until it passes
3. Run the full suite: `pytest` — nothing should have broken

## Endpoints

All under `/api/hotfix-booking`:

- `GET  /field-options`
- `GET  /deployed-cms`
- `GET  /next-version`
- `GET  /bookings`
- `POST /book`
- `GET  /client-versions`
- `GET  /history?minor=&major=`

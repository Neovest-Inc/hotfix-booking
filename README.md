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

```powershell
pytest
```

Tests are hermetic: Jira is mocked with `respx` against fixtures in `tests/fixtures/jira/`.
The bookings file used by tests is a per-test `tmp_path` — never touches `data/`.

## Endpoints

All under `/api/hotfix-booking`:

- `GET  /field-options`
- `GET  /deployed-cms`
- `GET  /next-version`
- `GET  /bookings`
- `POST /book`
- `GET  /client-versions`
- `GET  /history?minor=&major=`

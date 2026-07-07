# Hotfix Booking

A small internal web app that helps the team coordinate hotfix releases across clients and components. It reads live data from Jira, shows it in three useful views, and lets you reserve the next available hotfix version number so two people don't accidentally pick the same one.

## What can users do with it?

Open the app in a browser and you'll see three tabs:

### 1. Book Hotfix
- Shows the **next available hotfix version** (e.g. `9.97.82`), calculated from what's already been deployed in Jira plus what other people have already booked.
- Pick one or more **components** and one or more **client environments** from searchable dropdowns.
- Click **Book Hotfix Version** to reserve that version number for your work.
- The 10 most recent bookings appear underneath.

### 2. Version Matrix
A grid showing, for every **client** × every **component**, the highest version currently deployed. Handy for answering "what version of X is client Y running?" at a glance. Hover any cell to see the underlying Jira CM ticket and deployment date.

### 3. Hotfix History
A filterable table of every hotfix for a given minor version (e.g. `9.97.x`). Combines what's been **deployed** (from Jira) with what's been **booked** (from this app) into one chronological view. Every row with a CM ticket links straight back to it in Jira.

## How does it work?

- **Front-end**: a single static HTML/CSS/JavaScript page served from `/`.
- **Back-end**: Python (FastAPI) that talks to Jira on demand. Every screen refresh re-queries Jira for fresh data — nothing is cached.
- **Storage**: bookings are saved to a small JSON file at `data/hotfix-bookings.json`. Everything else lives in Jira.
- **No database, no login, no external services beyond Jira** — deliberately minimal.

Bookings clean themselves up automatically: once Jira shows a booked version has actually been deployed, the app removes it from the local bookings file on the next refresh.

## Running it locally

You need Python 3.11+ and Jira credentials.

```powershell
# One-time setup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
copy .env.example .env    # then edit .env with real Jira creds

# Start the app
uvicorn hotfix_booking.app:app --reload --port 3001
```

Open http://localhost:3001/.

## Running the tests

```powershell
pytest              # runs the full suite in about 3 seconds
```

There are two kinds of tests:

- **91 hermetic tests** — fast, deterministic, no internet needed. These cover every function and every HTTP endpoint against small hand-crafted test data.
- **8 "canary" tests** — parse *real* Jira responses to catch any change Atlassian might make to Jira's API. They skip themselves if no real data has been captured yet.

To refresh the canary against current Jira and re-run:

```powershell
python tools\capture_hotfix_fixtures.py
pytest
```

Do this before any release, and any time something feels off. Once the app is deployed to a shared server, this should run nightly on that server.

## Known limits

- Every booking is attributed to "Dashboard User" — there's no login yet.
- No history of who booked what beyond what's in the bookings file.

Abandoned bookings (booked but never made it to Jira as a CM) are auto-removed
after 180 days by default. Tune via `BOOKING_RETENTION_DAYS` in `.env`.

If any of these become a real problem they can be addressed. Ask before changing user-visible behavior.

## Project layout

```
src/hotfix_booking/    Python back-end
static/                HTML/CSS/JS served to the browser
tests/                 Automated tests + fixture data
tools/                 Utility scripts (fixture capture, side-by-side diff)
data/                  Bookings file lives here (created on first booking)
```

For developers or coding agents working on this project, see [CLAUDE.md](CLAUDE.md).

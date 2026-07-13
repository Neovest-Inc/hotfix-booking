# Hotfix Booking

A small internal web app that helps the team coordinate hotfix releases across clients and components. It reads live data from Jira, shows it in three useful views, and lets you reserve the next available hotfix version number so two people don't accidentally pick the same one.

## Signing in

On your first visit you'll see a **Log in with Atlassian** button. One click, one Atlassian consent screen, and you're in — the app reads your name and email straight from your Atlassian (Jira) profile, so every booking is attributed to the same name you appear under on CM tickets.

- No separate password — your Atlassian login is your login.
- Your session is kept in a **signed cookie in your browser** that lasts 365 days by default.
- Different browsers or a private window will prompt you to log in again.
- Click **Sign out** in the top-right to end your session on that browser.
- If your session ever expires mid-use, the app tells you and refreshes back to the login screen.

## What can users do with it?

Open the app in a browser and you'll see three tabs:

### 1. Book Hotfix
- Pick a **release line** (e.g. `9.98.x`, `9.97.x`, or even an older one like `9.93.x`) from the top dropdown. It auto-populates with the most recent active release lines from Jira, up to 8 of them, and when a new major arrives (e.g. `10.0.x`) it shows up there automatically.
- The **Next Available Version** badge shows the next hotfix number for that release line, calculated from what's already deployed in Jira plus what other people have already booked in this app.
- Pick one or more **components** and one or more **client environments** from searchable dropdowns.
- Click **Book Hotfix Version** to reserve that number for your work. The booking is attributed to whoever you signed in as.
- **My Hotfixes** (below the form) shows your latest 8 hotfixes on the selected release — either ones you booked in this app or ones where you're the Jira reporter of the CM. Same expandable tag lists as before. Refreshes automatically every 30 seconds while the tab is visible. See the **Hotfix History** tab for the release-wide view.

### 2. Version Matrix
A grid showing, for every **client** × every **component**, the highest version currently deployed. Handy for answering "what version of X is client Y running?" at a glance. Hover any cell to see the underlying Jira CM ticket and deployment date.

### 3. Hotfix History
A filterable table of every hotfix for a given release (up to 8 recent release lines in the dropdown). Combines what's been **deployed** (from Jira) with what's been **booked** (from this app) into one chronological view. Every row with a CM ticket links straight back to it in Jira.

## How does it work?

- **Front-end**: a single static HTML/CSS/JavaScript page served from `/`. On load it calls the server to check whether you have a valid session; if not, it shows the login gate and nothing else.
- **Back-end**: Python (FastAPI) that talks to Jira on demand. Every screen refresh re-queries Jira for fresh data — nothing is cached.
- **Storage**: bookings are saved to a small JSON file at `data/hotfix-bookings.json`. Everything else lives in Jira.
- **User identity**: proven by an **Atlassian OAuth 2.0 (3LO) login flow**. The server sees your Atlassian display name and email, stores them in a signed session cookie, and stamps every booking with them. Nothing you type in the browser can override this — no spoofing.
- **No database** — deliberately minimal.

Bookings clean themselves up automatically two ways:
- **Deploy-based**: once Jira shows a booked version has actually been deployed, the app removes it from the local file on the next screen refresh.
- **Age-based**: bookings that never turned into a real Jira CM are auto-removed after 180 days (configurable via `BOOKING_RETENTION_DAYS` in `.env`). This prevents the file from growing forever with abandoned bookings.

Two extra safety nets built in:
- **File lock**: two people clicking Book at the exact same second no longer risk losing a booking. Concurrent writes are serialized.
- **Server-side re-check**: when you click Book, the server re-queries Jira to make sure the version you're about to book is *actually* still the next available. If it's been taken while your tab was idle, you get a clear message and the badge updates.

## Running it locally

You need Python 3.11+ and Jira credentials, **plus** an Atlassian OAuth 2.0 app registered at [developer.atlassian.com](https://developer.atlassian.com/console/myapps/) with the `read:me` scope and callback URL `http://localhost:3001/api/hotfix-booking/auth/callback`.

```powershell
# One-time setup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
copy .env.example .env    # then edit .env with:
                          #   - Jira creds (JIRA_*)
                          #   - Atlassian OAuth app creds (ATLASSIAN_CLIENT_ID / _SECRET)
                          #   - A fresh SESSION_SECRET_KEY (see .env.example for the one-liner)
                          #   - APP_BASE_URL (defaults to http://localhost:3001)

# Start the app
uvicorn hotfix_booking.app:app --reload --port 3001
```

Open http://localhost:3001/, click **Log in with Atlassian**, approve the consent screen once, and you're in.

## Running the tests

```powershell
pytest              # runs the full suite in ~30 seconds
```

There are two kinds of tests:

- **Hermetic tests** — fast, deterministic, no internet needed. These cover every function and every HTTP endpoint against small hand-crafted test data, plus the OAuth flow with mocked Atlassian responses.
- **"Canary" tests** — parse *real* Jira responses to catch any change Atlassian might make to Jira's API. They skip themselves if no real data has been captured yet.

To refresh the canary against current Jira and re-run:

```powershell
python tools\capture_hotfix_fixtures.py
pytest
```

Do this before any release, and any time something feels off. Once the app is deployed to a shared server, this should run nightly on that server.

## Known limits

- **Single-tenant OAuth app**: the Atlassian OAuth 2.0 app is private by default — only the person who registered it can log in. Enable **Sharing** in the Atlassian Developer Console for teammates to log in too (they'll see a "not reviewed by Atlassian" banner on first consent, which is normal for internal integrations).
- **Session lifetime**: default 365 days. Rotating `SESSION_SECRET_KEY` in `.env` logs everyone out immediately — the recovery lever if a cookie ever leaks.
- Runs as a single uvicorn worker by default. If you ever scale to multi-worker, the in-process file lock needs to be swapped for an OS-level file lock (see the note in `store.py`).

If any of these become a real problem they can be addressed. Ask before changing user-visible behavior.

## Project layout

```
src/hotfix_booking/    Python back-end (FastAPI app + Atlassian OAuth login flow)
static/                HTML/CSS/JS served to the browser
tests/                 Automated tests + fixture data
tools/                 Utility scripts (fixture capture, smoke check, seed loader)
data/                  Bookings file lives here (created on first booking)
```

For developers or coding agents working on this project, see [CLAUDE.md](CLAUDE.md).

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

3. **Do not add dependencies without approval.** Current runtime: `fastapi`, `uvicorn`, `httpx`, `python-dotenv`, `tzdata` (Windows-only, for `zoneinfo` ET conversion in the Teams notifier). Dev: `pytest`, `pytest-asyncio`, `respx`. New packages need a real justification.

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
                    CLIENT_CONTEXT_ID, PORT, BOOKING_RETENTION_DAYS,
                    ADMIN_EMAILS, TEAMS_TARGET, TEAMS_WEBHOOK_URL_<NAME>,
                    APP_BASE_URL)
  jira_client.py    httpx wrapper for the 4 Jira endpoint families we use
                    (search-jql, project components, custom-field options,
                    user search)
  versioning.py     parse_version / compare_versions / is_semver
  dependencies.py   overlaps / compute_parents / direct_children /
                    cm_to_pseudo_booking — the DAG math underpinning booking
                    dependencies + cancel-rebase. `cm_to_pseudo_booking`
                    converts a Jira CM to a booking-shaped dict so external
                    (Teams-chat) CMs count as eligible parents.
  store.py          load/save/create/cancel bookings, threading-lock,
                    MalformedBookingsError, backfill migration for records
                    that pre-date the `parents` field
                    (booking record includes `bookedBy` + `bookedByEmail`,
                    `parents`, `originalParents`, `rebaseHistory`, `status`;
                    parent IDs may include `jira:<CM-KEY>` pseudo-IDs for
                    Jira CMs that came from outside the app).
                    create_booking + cancel_booking accept optional
                    `additional_priors` (pseudo-bookings) that the routes
                    layer builds from Jira CMs on the same release line.
  matrix.py         build_version_matrix
  history.py        merge_hotfixes (deployed + cancelled tombstones) /
                    derive_minor_versions (cross-major, top 8) /
                    calculate_next_version (optional major/minor filter) /
                    cleanup_bookings (deploy- + age-based, cancelled records
                    exempt from deploy branch)
  users.py          resolve_jira_user — picks the real user record from raw
                    Jira search results (filters out `qm:` service stubs)
  teams_notifier.py Best-effort Teams webhook bridge — posts an Adaptive Card
                    to TEAMS_WEBHOOK_URL on every successful /book and /cancel.
                    No-op when the URL is unset. Any HTTP failure is logged
                    and swallowed so bookings never fail because Teams did.
  routes.py         The 9 HTTP endpoints under /api/hotfix-booking
  models.py         Pydantic request/response models
static/             HTML/CSS/JS served to the browser (no-cache headers)
                    Includes a first-visit modal that gates the app until the
                    user resolves their Neovest email via /resolve-user.
tests/
  fixtures/jira/       Hand-crafted synthetic Jira responses (hermetic tests)
  fixtures/jira-live/  Real Jira responses (git-ignored; local only)
  test_*.py            One file per module + per endpoint group
  conftest.py          Shared fixtures: bookings_file, settings, client, mock_jira
tools/
  capture_hotfix_fixtures.py   Refreshes tests/fixtures/jira-live/
  check_user_lookup.py         One-shot Jira user-search sanity check
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
| GET  | `/deployed-cms`    | All CMs from the last 120 days (paginated across all Jira pages up to a 15-page / 1500-CM safety cap) |
| GET  | `/next-version` `?major=&minor=` | Next available hotfix version for the given release line (or current if omitted). Response also includes `currentMajor`, `currentMinor`, and up to 8 `minorVersions` for the release dropdown. **Auto-cleanup side effect** (deploy- + age-based). Uses `deployed_only=False` so newly-opened release lines with no deploys yet (e.g. brand-new 9.98.x) appear in the dropdown. |
| GET  | `/bookings` `?major=&minor=` | Local bookings, optionally filtered to a release line |
| POST | `/book`            | Create a booking. Validates semver, checks fresh next-version against Jira for the submitted `version`'s release line. If `bookedByEmail` is present, resolves it via Jira and stores the canonical `displayName` as `bookedBy` (client-sent `bookedBy` is ignored — prevents spoofing). Also computes `parents` (DAG dependency graph — see below) and stores it plus `originalParents` on the record. On success, enqueues a Teams notification via `BackgroundTasks` (see Teams bridge below). Returns 400 on invalid input, 409 with `currentNext` if the version is no longer the next available, 500 on Jira failure. |
| GET  | `/client-versions` | Client × component version matrix |
| GET  | `/history` `?major=&minor=` | Merged deployed + booked list for a given release line. Response includes `currentMajor`, `currentMinor`, `targetMajor`, `targetMinor`, `minorVersions`. Cancelled bookings are kept in the response as tombstones (`bookingStatus: "cancelled"`); deployed entries that also have a cancelled local booking carry `cancelledLocally: true`. |
| GET  | `/resolve-user` `?email=X` | Looks up a Jira user by email. Returns `{email, displayName, accountId}` or 404 if no active user matches. Filters out Jira's `qm:`-prefixed service stubs. Used by the front-end to gate booking on a real identity. |
| POST | `/cancel`          | Soft-delete a booking and rebase its direct downstream children. Body: `{bookingId, cancelledByEmail}`. Auth: (a) the booker's email (case-insensitive), OR (b) an email in the `ADMIN_EMAILS` allow-list, OR (c) the caller's Jira displayName matches the `reporter` on any Jira CM for the booking's version. Under the store lock: marks the record `status: "cancelled"`, then for every direct child (booking whose current `parents` include the cancelled id) recomputes its `parents` against the store as-if the cancelled record never existed and appends a `RebaseEvent` to its `rebaseHistory`. Version number is **burned** (cancelled records stay in the store, so `/next-version` keeps counting past them). On success, enqueues a Teams notification via `BackgroundTasks` (see Teams bridge below). Response: `{cancelled, affected: [{id, version, bookedBy, bookedByEmail, previousParentVersions, newParentVersions}], activeCmWarning}`. `activeCmWarning` is populated when a Jira CM matching the cancelled version is in an **in-flight** status — anything NOT in the terminal set `{Done, Deployment Completed, Global Review, Rollback, Rejected, Cancelled}` (case-insensitive). In-flight statuses include `Open`, `DL Approved`, `Today's Deployments`, `In Progress`, `Business Approved`, `QA Approved`, and any future workflow status. The UI surfaces this as an amber "Active CM in Jira" chip. Returns 400 on missing fields or unresolvable email, 403 on unauthorized caller, 404 on unknown id, 409 if already cancelled. |

Release-line dropdowns auto-discover the top 8 `(major, minor)` pairs across all majors currently active in Jira — so when `10.0.x` starts appearing in real data it shows up automatically alongside `9.99.x`.

## Current known behaviors — ask before changing

These are the app's current behaviors. Some are limitations, some are deliberate. If you spot one and think it should change, propose it and wait for approval — do not fix silently.

- No server-side authentication or sessions. User identity is **self-declared** — the user enters their email, we resolve it via Jira's user-search to get the canonical `displayName`, and we trust that they typed their own email. Someone determined could type a teammate's email; not a concern for an internal, low-stakes tool. Upgrading to real Jira OAuth would fix this but was rejected as heavy for the use case.
- **User identity persistence is client-side** — `localStorage` under keys `hotfixBooking.userEmail` and `hotfixBooking.userName`. Survives browser restarts on the same browser+device. Different browsers / incognito / other devices re-prompt.
- **First-visit modal** in `static/index.html` (`#hbUserModal`) gates the whole app until the email resolves. The header "Booking as: … [change]" widget is the inline switcher after first visit.
- Every booking record has `bookedBy` (Jira displayName, canonicalized) plus `bookedByEmail` (as typed, for audit). Both are saved to `data/hotfix-bookings.json` and shown in the My Hotfixes / Hotfix History views.
- **Booking dependency graph.** Each booking carries `parents` (parent IDs on the same release line whose (client, component) cells it overlaps with), `originalParents` (snapshot at creation time — never mutated), and `rebaseHistory` (append-only log of upstream cancellations that rewired this booking's `parents`). Two bookings A, B overlap iff `A.clients ∩ B.clients ≠ ∅` AND `A.components ∩ B.components ≠ ∅`. `parents` is computed as the deduped set of most-recent non-cancelled priors, one per cell. Parent IDs can be either local booking IDs (`HB-<epoch>`) or Jira CM pseudo-IDs (`jira:<CM-KEY>`) — CMs on the same release line count as eligible parents so bookings made through the app stack correctly on top of hotfixes filed via the legacy Teams-chat flow. Only CMs in the **negative-terminal** set `{Rollback, Rejected, Cancelled}` are excluded (they never shipped). Shipped-terminal CMs (`Done`, `Deployment Completed`, `Global Review`) and every in-flight status (`Open`, `DL Approved`, `Today's Deployments`, `In Progress`, `Business Approved`, `QA Approved`, plus any future status) count as valid parents. When a local booking and a Jira CM share the same version, the local record wins. Empty `parents` means "based on the baseline `major.minor.0`". Legacy records without these fields are backfilled by `store.load_bookings` in `bookedAt` order — idempotent, records already having `parents` are trusted (never overwritten), so cancelling doesn't recompute pre-existing rebases and reintroducing the CM-priors bridge doesn't retroactively rewrite old bookings.
- Booking auto-cleanup runs as a side effect of `GET /next-version` (both deploy-based and age-based, threshold `BOOKING_RETENTION_DAYS`, default 180). Cancelled records are **exempt from deploy-based cleanup** (audit tombstones) but still age-out.
- Concurrency: writes are serialized by a `threading.Lock` in `store.py`, which is safe for a single uvicorn worker. Multi-worker deployments would need an OS-level file lock (`filelock` package) — swap the `bookings_lock()` implementation in `store.py`
- All user-facing timestamps in the UI are shown in Eastern Time (`America/New_York`) and suffixed with " ET"
- **Teams bridge to the hotfix chat.** Every successful `/book` and `/cancel` posts an Adaptive Card to a Power Automate / Teams Workflow "Send webhook alerts to a chat" URL so the hotfix Teams group chat receives announcements. Runs via FastAPI `BackgroundTasks` after the response is prepared. The active webhook is chosen by `TEAMS_TARGET=<name>`, which resolves to `TEAMS_WEBHOOK_URL_<NAME>` in the env (conventional names: `prod`, `test`). If `TEAMS_TARGET` is unset OR the matching URL var is empty, notifications are disabled — we never silently fall back to a different URL (would risk posting test messages to the wrong chat). Card timestamps are rendered in Eastern Time via `zoneinfo` (needs the `tzdata` package on Windows). If Teams returns an error or times out (5s), the failure is logged at WARNING and swallowed; the booking / cancel still succeeds. The `APP_BASE_URL` env var, if set (no trailing slash), controls whether cards include an "Open in Hotfix Booking tool" button pointing back at the app. Treat all `TEAMS_WEBHOOK_URL_*` values as secrets — anyone with them can post into the chat.

## Common pitfalls

- **Do not overwrite `tests/fixtures/jira/` with real Jira data.** Those files are hand-crafted synthetic fixtures, and the hermetic tests assert exact values from them. Real Jira data belongs in `tests/fixtures/jira-live/`.
- **When testing endpoint behavior that depends on Jira's JQL filter** (e.g. `status in ("Deployment Completed", "Done")`), simulate the filter in your respx `side_effect` — don't blindly return the same fixture for every JQL. Otherwise you'll fake-pass tests that would fail against real Jira. See `test_dropdown_includes_lines_with_only_in_progress_cms` for the pattern.
- **Run `node --check static/hotfix-booking.js` after every JS edit.** JS has no compile step and browsers may silently drop a whole IIFE on a syntax error — the pytest suite won't catch this. It burned us once already.
- **Jira `user-search` returns extra stub accounts** (accountId prefixed `qm:`, empty `emailAddress`) alongside the real user. Always filter through `resolve_jira_user()` — don't consume the raw response.
- **When tests fail, read them.** Test names are descriptive and reveal the intended behavior.
- **`data/hotfix-bookings.json` is git-ignored** (per `.gitignore` — `data/*` except `.gitkeep`). Don't commit it.
- **Static assets have cache-busting `?v=N` query strings** in `static/index.html`. Bump the number when making a coordinated JS+HTML change that could otherwise break with stale-JS/new-HTML. `NoCacheStaticFiles` in `app.py` also sends `Cache-Control: no-store` headers as a belt-and-suspenders measure.

## Style

- Concise responses; no emojis unless the user asks
- Verify state (`git status`, file contents, test output) before making changes
- End of turn: one short summary of what changed. No recap lists or "I also did..." tails.
- When the user asks a direct question, answer it directly first, then act

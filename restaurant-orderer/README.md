# restaurant-orderer

Watches table availability for a restaurant on **Ontopo** and **Tabit**. It runs once a
day and sends an email digest **only when at least one open slot was found**. Each slot in
the email has a one-click reservation link. Optionally it can book automatically, but only
where the platform needs no SMS code and no card guarantee (see below).

The bootstrap config watches **Afrit** for a party of 2. It covers **12.11.2026 – 30.12.2026**,
weekends only (Friday and Saturday by default), from 19:00 to 22:30.

> ⚠️ The Ontopo and Tabit endpoints were written without network access and are
> **unverified**. Before the first real run, read [`docs/research.md`](docs/research.md),
> confirm the endpoints in DevTools and fill in Afrit's venue id. Until a venue id is
> filled in, the run logs `not_configured` and sends no email.

## Layout

```text
restaurant-orderer/
├── README.md
├── config.json        # venue, date range, weekend filter, party size, time window, email, platform endpoints
├── .env.example       # SMTP + contact settings template (copy to .env; .env is git-ignored)
├── requirements.txt
├── main.py            # daily entry point: scan -> optional auto-order -> conditional email
├── settings.py        # config loading/validation, .env loader, weekend-date iteration
├── searcher.py        # Ontopo + Tabit adapters behind one interface, rate-limited HTTP client
├── notifier.py        # plain-text + HTML digest, sent only when slots exist
├── orderer.py         # auto_order feasibility check with fallback to deep links
├── docs/research.md   # Phase 2 endpoint research (unverified live)
├── pytest.ini
└── tests/             # offline tests with JSON fixtures; never touch the network or SMTP
```

## Setup

```bash
cd restaurant-orderer
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip install -r requirements.txt
cp .env.example .env                              # then fill in SMTP + recipients
.venv/bin/python main.py --dry-run                # scan and print the email instead of sending it
.venv/bin/python -m pytest -q                     # run the tests
```

Secrets (SMTP password and contact details) come **only** from environment variables or
`.env`. Real environment variables win over `.env`. For Gmail, use an App Password.

| Variable | Purpose |
|---|---|
| `RO_SMTP_HOST`, `RO_SMTP_PORT` | SMTP server (587 = STARTTLS, 465 = SSL) |
| `RO_SMTP_USER`, `RO_SMTP_PASSWORD` | SMTP login |
| `RO_EMAIL_FROM`, `RO_EMAIL_TO` | sender; comma-separated recipients |
| `RO_CONTACT_NAME`, `RO_CONTACT_PHONE`, `RO_CONTACT_EMAIL` | used only by `auto_order` |

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `restaurant_name` | Name shown in the email (`"Afrit"`) |
| `date_range.start` / `.end` | Inclusive ISO dates. Past dates are skipped automatically. |
| `days_filter` | `["weekend"]`, `["all"]` or explicit day names such as `["thursday", "friday"]` |
| `weekend_days` | What "weekend" means. The default is `["friday", "saturday"]`. Use `["thursday", "friday", "saturday"]` to include Thursday evening. |
| `party_size` | 1–30 |
| `mode` | `"notify_only"` (default) or `"auto_order"` |
| `time_window` | `start` / `end` (HH:MM) filter the reported slots. `query_times` are the anchor times sent to the platforms; each search returns slots about ±1–2 h around its anchor. |
| `time_window_overrides` | Per-weekday window, for example a later start on Thursday |
| `email_notifications` | `frequency: "daily"` (at most one digest per day; `--force` overrides). `suppress_empty: true` means no email at all on days with zero slots. |
| `http` | Rate limit, jitter, timeout, retries, backoff, User-Agent |
| `platforms.ontopo` / `platforms.tabit` | `enabled`, the venue id (`venue_slug` / `organization_id`), endpoint URLs, deep-link template. **Unverified.** |

## How a run works

1. Build the list of target dates: the weekend days in `[start, end]`, starting from today
   (Asia/Jerusalem).
2. For each enabled and configured platform and each date, query every `query_time` and keep
   the slots inside the window. Duplicates are merged and the result is sorted by
   date/time/area.
3. Every platform problem becomes a status and never a crash: `blocked` (401/403/429/captcha),
   `error`, `unknown_response` or `not_configured`. Once a platform is `blocked`, it is
   skipped for the rest of the run.
4. `orderer`: in `auto_order` mode, it books only if the adapter says it needs no OTP and no
   card, and contact details are set. Otherwise it logs why and falls back to the
   notification. It never books twice (`.state/booking.json`).
5. `notifier`: if there are 0 slots and `suppress_empty` is set, it exits 0 with **no email**.
   Otherwise it sends one plain + HTML email listing the restaurant, each date and time, the
   seating area, and a Reserve deep link.

The exit code is 0 for every normal outcome and 2 for an invalid config.

CLI flags: `--dry-run` (no email, no booking; prints the digest), `--today YYYY-MM-DD`,
`--config PATH`, `--env PATH`, `--force`, `-v`.

## Scheduling (daily)

### macOS / Linux (cron)

`crontab -e`, then add one line (this example runs every day at 09:00 local time):

```cron
0 9 * * * cd /path/to/ComputerAutomations/restaurant-orderer && .venv/bin/python main.py >> .state/cron.log 2>&1
```

Create the log folder first with `mkdir -p .state`. On macOS, cron may need
*Full Disk Access* (System Settings → Privacy & Security). Another option there is a
`launchd` agent with `StartCalendarInterval`.

### Windows (Task Scheduler)

PowerShell, run once:

```powershell
$dir = "C:\path\to\ComputerAutomations\restaurant-orderer"
$action  = New-ScheduledTaskAction -Execute "$dir\.venv\Scripts\python.exe" -Argument "main.py" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At 9:00am
Register-ScheduledTask -TaskName "RestaurantOrderer-Afrit" -Action $action -Trigger $trigger `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
```

You can also do this in the GUI: Task Scheduler → Create Basic Task → Daily → Start a program.
Set the program to `.venv\Scripts\python.exe`, the arguments to `main.py`, and "Start in" to
the folder. `-StartWhenAvailable` runs a missed run after the machine wakes up.

## Auto-ordering: why it's off

Both Ontopo and Tabit appear to require an **SMS one-time code** to complete a booking, and
many venues add a **credit-card guarantee**. Both adapters therefore declare
`otp_required=True`. With `mode: "auto_order"` the run logs the reason, adds a note to the
email and falls back to deep links. The booking path itself is implemented and tested with
fake adapters, so a platform without these barriers only needs `book()` and a
`booking_requirements()` that returns no OTP and no card. See `docs/research.md`.

## Work breakdown (Phase 4)

The issue asked for 4 follow-up GitHub issues. They have **not** been created; this is the
breakdown and its status:

| # | Item | Status |
|---|---|---|
| 1 | **Scaffolding & config setup**: folder, requirements, `config.json` set up for Afrit (12.11–30.12, weekends) | ✅ Done |
| 2 | **API reverse engineering (Ontopo/Tabit)**: map the availability endpoints, headers and params for Afrit | 🟡 Partial. Adapters, config-driven endpoints and fixtures exist, but the endpoints are **unverified live**, and Afrit's platform and venue id still need to be looked up (see `docs/research.md`). |
| 3 | **Conditional daily email pipeline**: daily scan; email only when slots are found | ✅ Done. Tested offline; needs real SMTP creds in `.env`. |
| 4 | **Auto-scheduling & deep linking**: deep links plus an optional auto-reservation runner | ✅ Deep links and the feasibility/fallback logic are done. Real `book()` implementations are intentionally absent, because both platforms need OTP. |

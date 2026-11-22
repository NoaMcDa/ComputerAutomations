# Facebook Marketplace deals in the daily email (issue #54)

> **Status: implemented, live behaviour UNVERIFIED.** This document and the
> code were written without internet access. Everything said here about how
> Facebook's pages look or behave comes from general knowledge of the
> Marketplace web app and of public open-source scrapers, not from a request
> made while writing it. The first real run will tell whether logged-out
> search still returns listings from your connection; until then treat the
> "approach" below as the best-guess plan, not a confirmed result.

Code: `src/price_watcher/marketplace.py` · tests: `tests/test_marketplace.py`
(offline, synthetic fixtures in `tests/fixtures/marketplace/`).

---

## Phase 1 — research

### Scraping strategy

| Option | Verdict |
|---|---|
| Official API (Graph API) | **Not available.** There is no public API to search Marketplace listings; the Marketplace partner APIs are for sellers / catalog partners only. |
| Plain HTTP request to the search URL (httpx) | **Unlikely to work.** The search page is a React/Relay app; a non-browser client typically gets a near-empty shell or a login redirect. It is cheap to try but not the main path. |
| Calling Facebook's internal GraphQL endpoint (`/api/graphql/` with a `doc_id`) | **Rejected.** It needs tokens scraped from a session, the `doc_id`s change without notice, and it is the most clearly "automated access" pattern — highest ban risk for the lowest stability. |
| Logged-in browser session (cookies from your account) | **Rejected by default.** It works best, but automating your personal account risks a checkpoint / account restriction, and storing your session cookies in a scheduled job is a security liability. |
| **Headless Chromium (Playwright), logged out, one page load per query** | **Chosen.** Lowest friction that has a chance of working: Playwright is already a dependency (iHerb and the generic fetcher use it), it executes the page's JS, and one daily load per query is a very low request rate. |

How the chosen approach reads the page:

1. Open `https://www.facebook.com/marketplace/<location>/search/?query=<q>&maxPrice=<n>&exact=false`
   (`<location>` is a city slug such as `telaviv` or a numeric location id;
   without it Facebook picks a location from the IP address). URL shape is
   from memory — unverified.
2. Wait for listing links (`a[href*="/marketplace/item/"]`).
3. Parse the **embedded Relay JSON** first: the server-rendered page has
   `<script type="application/json">` blobs containing objects with
   `id`, `marketplace_listing_title`, `listing_price.amount` /
   `formatted_amount`, `location.reverse_geocode.{city,state,city_page}`,
   `is_sold`. The parser walks every JSON blob for any object with a
   `marketplace_listing_title`, so it does not depend on the exact nesting.
4. Fallback: **DOM cards** — every `<a href="/marketplace/item/<id>/">`
   whose visible text lines are *price, (old price), title, location*.
5. If neither finds listings, the page is classified:
   redirect to `/login` or a login form → `login_wall`;
   `/checkpoint/`, "temporarily blocked", HTTP 403/429 → `blocked`;
   otherwise "no results" (logged at INFO, because a changed markup and an
   empty result look the same).

Statuses never raise: a login wall or block shows up as a red line in the
Marketplace section of the email, and the rest of the daily email is sent
as usual.

### Data & state storage

**Local JSON** (`marketplace_state.json` in the app-data folder, next to the
other state files) — SQLite would be overkill for a few hundred listings and
the project already uses atomic JSON files everywhere.

```json
{
  "schema_version": 1,
  "queries": { "iphone 13": { "baseline_at": "...", "last_ok_at": "..." } },
  "items": {
    "1111111111111111": {
      "item_id": "1111111111111111", "title": "...", "price": "1850",
      "currency": "ILS", "url": "https://www.facebook.com/marketplace/item/1111111111111111/",
      "location": "Ramat Gan, Tel Aviv District",
      "first_seen": "...", "last_seen": "...", "query": "iphone 13"
    }
  }
}
```

* Writes are atomic (temp file + `fsync` + `os.replace`).
* State is written **only after the daily email was sent** (same rule as
  the creatine / Sims 4 / supermarket stages), so a failed send does not
  swallow a drop.
* Listings not seen for `PW_FB_FORGET_DAYS` (30) days are dropped.

### Price-delta logic

* **Placeholder prices are noise**: a price at or below `PW_FB_NOISE_MAX`
  (default ₪1 — the ₪0 / ₪1 "message me" listings) is never stored as a
  price, never overwrites a real stored price and never produces a deal.
  "Free" parses as ₪0 and is therefore treated as a placeholder too.
* **A drop counts** only when both are true:
  `old − new ≥ PW_FB_MIN_DROP_ILS` (default ₪20) **and**
  `(old − new) / old ≥ PW_FB_MIN_DROP_PCT` (default 5 %).
* Rises are stored silently.
* **New listings** are reported (unless `PW_FB_REPORT_NEW=0`) only for a
  query that already has a baseline: the first successful run of a query
  records every listing silently, so the first email is not a flood.
* Deals above the query's max price are not reported. Drops come first
  (biggest % first), then new listings (cheapest first), capped at
  `PW_FB_MAX_ITEMS` (20); the rest is summarised as "+N more".

### Limits and risks (read before relying on it)

* **Terms of Service.** Facebook's terms prohibit collecting data by
  automated means without permission. This feature is a personal, low-rate
  (one page per query per day), logged-out read of public search results,
  but it is still automated access: use it at your own risk, keep the query
  list short, and do not raise the frequency.
* **Login wall.** Facebook often hides Marketplace behind a login prompt for
  logged-out visitors, increasingly so for headless browsers and datacenter
  IPs. It may work from a home connection and stop working at any time. The
  feature reports `login_wall` and keeps going; it does **not** log in.
* **Blocking.** Repeated loads can trigger "You're temporarily blocked" or a
  checkpoint. Reported as `blocked`.
* **Markup changes.** Field names in the embedded JSON and the card layout
  change without notice. The JSON walker and DOM fallback are deliberately
  loose, but a silent "no listings" is possible; check the log if the
  section stays empty for days.
* **Location.** Logged-out results are localised by IP unless a location
  slug is given; the radius parameter is a guess.
* **Re-listings.** A seller who deletes and re-posts gets a new item id; it
  appears as a new listing, not as a drop.
* Only the first page of results (what loads without scrolling, roughly
  20–30 cards) is read.

---

## Phase 2 — the email section

Section title in the (Hebrew, RTL) daily email: **"Facebook Marketplace — מציאות"**.
For each deal: title (linked to the listing), price (`₪150 → ₪120`, struck-through
old price and the % change for drops; "חדש" for new listings), and the
location / pickup area when Facebook gives one.

* Empty state (feature on, nothing found): one line —
  **"לא זוהו היום מציאות חדשות ב-Marketplace."** (Hebrew equivalent of
  "No new Marketplace deals detected today.", matching the rest of the
  email).
* Failed queries (login wall / blocked / Playwright missing) are listed
  under the section as one line each.
* Feature off (`PW_FB_QUERIES` empty or unset): the section is **omitted**.

Plain HTML table with inline styles, same colours as the other sections; no
templating dependency.

Hooks into shared files (kept to a few clearly-marked lines):
`digest.py` (field on `DailyReport`, stage call in `collect_daily_report`,
terminal print, `persist` after a successful send) and `email_sender.py`
(`section_text` / `section_html` in `build_daily_payload`).

## Configuration

```dotenv
# ;-separated queries; optional per-query max price after "|"
PW_FB_QUERIES=iphone 13|2000;כיסא משרדי;אופניים חשמליים|3500
PW_FB_LOCATION=telaviv        # optional location slug / id
PW_FB_RADIUS_KM=20            # optional, unverified URL parameter
PW_FB_MAX_PRICE=5000          # default max price for queries without one
PW_FB_NOISE_MAX=1             # prices <= this are placeholders
PW_FB_MIN_DROP_ILS=20
PW_FB_MIN_DROP_PCT=5
PW_FB_REPORT_NEW=1
PW_FB_MAX_ITEMS=20
PW_FB_FORGET_DAYS=30
```

Try it without sending email or changing state:
`price-watcher marketplace-preview`. Full dry run of the daily email:
`price-watcher digest --dry-run`.

## Scheduling

No separate job is needed: the Marketplace stage runs **inside the existing
daily report job** (`price-watcher digest`, installed with
`price-watcher install-digest-schedule` — launchd on macOS, Task Scheduler on
Windows). On Linux, where the installer does not set up a schedule, add a
crontab line (`crontab -e`), adjusting the path to your virtualenv:

```cron
# daily report incl. Facebook Marketplace, 09:00 local time
0 9 * * * cd /path/to/price-watcher && .venv/bin/price-watcher digest >> "$HOME/.local/state/price-watcher/cron.log" 2>&1
```

Logging goes through the project's normal logger (`log_dir()`); each query's
status and listing count is logged at INFO, failures at WARNING.

---

## Phase 3 — work breakdown (instead of creating GitHub issues)

The issue asked for 3–4 GitHub issues. None were created (this work was done
offline and without GitHub access by design); here is the breakdown with its
status so it can be copied into issues if still wanted.

| # | Item | Status |
|---|---|---|
| 1 | **Scraper script** — extract id, title, price, link, location for each configured query (Playwright, logged out; embedded JSON + DOM fallback; login wall / block as status) | Done in code, unit-tested on synthetic pages. **Live access unverified** — first real run needed. |
| 2 | **Local state & price diff** — `marketplace_state.json` with atomic writes; noise filter; min ₪ / % thresholds; baseline on first run; forget stale listings | Done, unit-tested. |
| 3 | **Email section** — "Facebook Marketplace" block in the daily email (text + HTML), empty-state line, omitted when unconfigured, failures listed | Done, unit-tested (including the daily-payload hook). |
| 4 | **Scheduling, logging, error handling** — runs inside the existing daily job; crontab line for Linux documented above; per-query logging; every failure is a status, never an exception that would stop the daily email | Done. No separate cron job required. |

Follow-ups that need a live run: confirm the URL shape and location slug,
confirm that logged-out search returns listings from the home connection,
and, if the field names differ, update `_listing_from_json` / the fixture.

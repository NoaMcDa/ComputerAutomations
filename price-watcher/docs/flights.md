# Flight price watching — El Al and Arkia (issue #10)

> **Status: logic implemented and unit-tested; live behaviour UNVERIFIED.**
> This work was done with no internet access, so the survey the issue asks
> for (task 1) could **not** be carried out. The airline adapters are
> written against guessed URLs and a generic parser. They report `blocked` or
> `unsupported` instead of crashing, so nothing breaks, but no El Al or Arkia
> price has actually been read yet. The "Done when" condition of the issue is
> therefore met only in its fallback form: this document records why it
> cannot yet be confirmed and what the alternatives are.

Code: `src/price_watcher/flights.py` (model, window, matching, rules,
storage, runner, adapters), `src/price_watcher/cli_extras.py` (commands),
tests: `tests/test_flights.py` (fake `AirlineSource`, no network).

---

## 1. Survey plan (to run from the home computer)

None of this has been checked. For **each airline** (El Al `elal.com`,
Arkia `arkia.co.il`), open a one-way search TLV → a common destination in a
normal browser with DevTools → Network open, and record:

| Question | How to check | What it decides |
|---|---|---|
| (a) Is the search-result URL stable? | Copy the URL after searching, open it in a private window. Does it reproduce the same search? | Whether the adapter can deep-link (`search_url`) or must fill the form with Playwright. |
| (b) Is there a monthly / low-fare calendar? | Look for "flexible dates", a fare calendar, or a ±3-day bar on the results page; note the XHR that fills it. | If yes: one request covers the whole ±15-day window → implement `fare_calendar()`. |
| (c) Are flight numbers shown? | Results list / flight details. Also check the JSON response for `flightNumber`-like fields. | Matching by number vs. by departure time only. |
| (d) Does headless Chromium load it? | `PWDEBUG=1 price-watcher add-flight --airline elal ...`, then headless; compare. Watch for CAPTCHA / "Access denied" / an Imperva, Akamai, Cloudflare or PerimeterX interstitial. | Whether scraping is viable at all. |
| (e) Which XHR carries the results? | Network tab → filter Fetch/XHR → find the response with prices. Save it (sanitised) as a test fixture. | Fill in the real field names in `options_from_payloads` (or a dedicated parser). |

Record the findings in this file (section 2) and in a fixture under
`tests/fixtures/flights/`.

## 2. Known facts and risks (from general knowledge — unverified)

* **Pages are built by JavaScript.** Both airlines run single-page booking
  flows; prices come from XHR/JSON calls after the search form is
  submitted. Plain HTTP fetching of the results URL will not work; a real
  browser engine is needed, which is why the adapters use Playwright and
  capture the JSON responses rather than scraping the DOM.
* **Bot protection.** Airline booking engines are among the most heavily
  protected sites (commercial bot-management such as Akamai, Imperva or
  similar is common in the industry; which one each airline uses is not
  verified). Expect CAPTCHAs or silent empty results for headless browsers,
  especially for repeated hourly searches. The adapters detect common
  interstitial markers and HTTP 403/429 and return `blocked`.
* **Search URLs.** El Al's booking flow has historically run on a
  third-party booking engine; deep-link parameters may be POST-only or
  session-bound. Arkia sells largely domestic (Eilat) and charter-style
  routes with its own booking UI. The deep-link URLs in `ElAlSource` /
  `ArkiaSource` are **guesses** and marked as such in the code.
* **Fare calendars.** Low-cost and leisure carriers often show a
  cheapest-per-day calendar; full-service carriers often show a ±3-day
  strip. If either exists, a single request can cover much of the window;
  otherwise the window costs one search per day (30 searches), which is the
  main reason the window is checked less often (below).
* **Price definition.** Prices differ by fare family (Lite / Classic /
  Flex, with or without bags). "The price" is defined as the **cheapest
  total fare for the flight for all passengers** — the parser keeps the
  minimum per flight.
* **Currency.** El Al may quote in USD depending on site/locale; each option
  carries its currency, and the email shows it.

## 3. Alternative data sources (evaluation, unverified)

If scraping turns out to be unreliable (likely for hourly checks):

| Source | Covers LY / IZ? | Access | Notes |
|---|---|---|---|
| **Amadeus for Developers** (Flight Offers Search, Flight Cheapest Date Search) | Likely El Al (GDS carrier); Arkia uncertain | Free self-service tier with monthly quota | Has a "cheapest date" endpoint that fits the ±15-day window. Self-service tier availability / future should be checked — it has been changing. Test environment uses cached, not live, data. |
| **Duffel** | El Al possibly via NDC/GDS; Arkia unlikely | Account + per-booking pricing; search is free-ish | Booking-oriented API; good data quality where covered. |
| **Google Flights** (no official API) / SerpApi-style resellers | Usually both | Paid third-party scraping APIs | Covers both airlines in Google's UI; indirect scraping, ToS risk moves to the vendor. |
| **Kiwi.com Tequila** | Mixed | Partner programme, increasingly closed to individuals | Not recommended for a personal tool. |
| **Skyscanner** | Usually both | Partner-only API | Not available to individuals. |
| **Travelpayouts / Aviasales data API** | Partial | Free with affiliate account | Returns *cached* cheapest prices per day — suitable for the window trend, not for the exact pinned-flight price. |

Recommendation: after the survey, if a headless browser is blocked, add an
`AirlineSource` backed by a cheapest-date API (Amadeus first) for the
**window**, and keep the pinned flight on the airline site at a low rate
(or accept the API's offer for that flight number). The pluggable
`AirlineSource` interface exists for exactly this swap; nothing else in the
module depends on Playwright.

## 4. What is implemented

### Watch model (`FlightWatch`, stored in `flight_watches.json`)
airline, origin, destination, pinned date, stored flight identity (flight
number when the source exposes it + scheduled departure time), passengers,
optional **stated** return date (round trip only when stated; it never moves
with the outbound date), change threshold (₪30 **and** 3 % by default — the
threshold is on by default for flights), active flag.

Flight watches live in their own file next to `watchlist.json` rather than
inside it, so the shop-item watch list format is untouched.

### `add-flight` (pick-from-a-list)
```
price-watcher add-flight --airline elal --from TLV --to LHR --date 2026-12-20 [--passengers 2] [--return 2026-12-27] [--min-change 30]
```
Loads the day's flights from the source and shows a numbered list
(departure, arrival, flight number, price); the user picks a number. The
user never types a flight number. If the source is blocked / unsupported,
the command says so and exits 1. Also: `list-flights`, `remove-flight <id>`,
`check-flights [--dry-run]`.

### Window
`window_dates()` — pinned date ±15 days, past dates skipped; once the
pinned date has passed the watch is marked ended and no longer checked.

### Matching the pinned flight
`match_flight()` — normalised flight number first (`LY 001` = `LY1`), then
the closest departure time within 60 min (wraps midnight). No match →
**"flight no longer listed"** (`not_listed`), reported as a state change,
never as a price change.

### States, not errors
Per date: `ok`, `no_flights`, `sold_out`. Fetch problems (`blocked`,
`unsupported`, `error`) keep the last known value for that date; after 3
consecutive problems on the pinned flight one alert email is sent.

### Per-date storage (`flights_state.json`)
For each watch: pinned status/price, per-date `{status, cheapest price,
checked_at}`, window cheapest, dates currently cheaper than the pinned
flight, last window check. Atomic writes. State for a watch advances only
after its email was sent (a failed send is retried next run).

### Notification rules (`evaluate`) — one combined email per watch per run
* first run = baseline only;
* pinned price moved by at least the threshold;
* pinned flight changed state (no longer listed / sold out / no flights /
  available again);
* the cheapest date in the window changed (another date at a different
  price, or the same date moving by at least the threshold);
* a date other than the pinned one became cheaper than the pinned flight
  by at least the threshold (each date reported once);
* the email lists the pinned price, the cheapest date and price, and the
  next 3 cheapest dates.

### Cadence
`check-flights` checks the pinned flight on every run. The window is
refreshed at most every 6 hours (`window_interval_hours`): one
`fare_calendar()` call when the source has one, else one search per date.
If the pinned search was blocked, the window is skipped; if a window search
is blocked, the remaining dates are skipped for that run.

Scheduling is not installed automatically for flights (the shared scheduler
is out of scope here). Suggested Linux crontab line:

```cron
# hourly flight check (window refreshed every 6 h internally)
15 * * * * cd /path/to/price-watcher && .venv/bin/price-watcher check-flights >> "$HOME/.local/state/price-watcher/flights-cron.log" 2>&1
```

On macOS/Windows, add an equivalent launchd / Task Scheduler entry running
`price-watcher check-flights`. Given the bot-protection risk, start with a
few runs a day, not hourly.

### Adapters (`ElAlSource`, `ArkiaSource`) — UNVERIFIED
Headless Chromium loads the guessed results URL, captures JSON responses,
and `options_from_payloads()` looks for any object with a flight number, a
departure time and a price (field-name lists are guesses). Outcomes:
Playwright/Chromium missing → `unsupported`; CAPTCHA / bot interstitial /
HTTP 403/429 → `blocked`; nothing recognised → `unsupported` with a pointer
to this document; "no flights" text → `no_flights`. They never raise.

## 5. Task checklist (issue #10)

| Task | Status |
|---|---|
| Survey (a)–(d) | **Not done — needs internet / home computer.** Plan in §1. |
| Flight watch type | Done (`FlightWatch`, separate `flight_watches.json`). |
| `add-flight` with pick-from-list | Done (driven by the source; tested with a fake source). |
| Playwright extraction (pinned + window) | Scaffolded, **unverified**; returns clear statuses. |
| Lower frequency for the window | Done (6-hour throttle; calendar used when available). |
| Price per date | Done. |
| Drop past dates / stop after pinned date | Done. |
| Minimum-change threshold on by default | Done (₪30 and 3 %). |
| Sold out / no flights as a state | Done. |
| Fallback documentation + data-source evaluation | Done (§2, §3). |

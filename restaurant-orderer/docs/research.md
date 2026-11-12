# Phase 2 research: Ontopo and Tabit endpoints

> **Status: NOT VERIFIED LIVE.** This was written without network access, so no request
> was made to ontopo.com, tabitisrael.co.il or tabit.cloud. Everything below comes from
> what is known about how the two public web clients work. Treat each endpoint, field
> name and response shape as an assumption that has to be checked once in browser
> DevTools. All URLs, paths and venue ids live in `config.json`, so you can fix them
> without changing code. The parsers accept small variations in the response and report
> `unknown_response` when the shape doesn't match, instead of crashing.

## Which platform does Afrit use?

This is unknown. Open the restaurant's website or Instagram "Reserve" link and see whether
it goes to `ontopo.com/...` or `tabitisrael.co.il/...`. Then:

* fill in the matching id in `config.json`, and
* set `"enabled": false` on the other platform. That avoids useless requests and keeps a
  permanent `not_configured` line out of the logs.

If both ids stay empty, the daily run logs `not_configured` for both platforms and exits
without sending an email.

## How to verify (about 5 minutes per platform)

1. Open the venue's booking page in Chrome and press F12, then go to Network and choose
   Fetch/XHR. Turn on "Preserve log".
2. Choose party size 2, a Friday in range and a time, then press search.
3. Find the request that returns time slots. Right-click it and choose Copy as cURL, then
   compare it with the assumptions below.
4. Fix `config.json` → `platforms.<name>` (endpoints, venue id). If the response field names
   differ, adjust `OntopoAdapter.parse` / `TabitAdapter.parse` and add the real response
   (anonymised) as a new fixture under `tests/fixtures/`.

## Ontopo (ontopo.com)

| Item | Assumption | Config key |
|---|---|---|
| Venue id | The page slug in `https://ontopo.com/he/il/page/<slug>` (often numeric) | `venue_slug` |
| Anonymous session | `POST /api/loginAnonymously` → `{"jwt_token": "...", "refresh_token": "..."}`. The token is sent back as the request header `token`. | `auth_endpoint` |
| Availability | `POST /api/availability/searchAvailability` | `availability_endpoint` |
| Request body | `{"slug": "<venue>", "locale": "he", "criteria": {"size": "2", "date": "YYYYMMDD", "time": "HHMM"}}` | built in code |
| Response | `{"areas": [{"id", "name", "options": [{"time": "HHMM", "method": "seat" \| "standby" \| "disabled", "text"}]}], "recommended": [...]}` | parser |
| Bookable | `method == "seat"` only. `standby` means the wait-list. | `OntopoAdapter.BOOKABLE_METHODS` |
| Deep link | `https://ontopo.com/he/il/page/<slug>?date=YYYYMMDD&time=HHMM&size=N` | `deep_link_template` |

A search only returns options around the requested time (about ±1–2 h). That's why
`time_window.query_times` holds two anchors (19:30 and 21:00) per evening.

**Booking feasibility.** Ontopo's checkout asks for name, phone and email, then verifies
the phone number with an SMS one-time code. Many venues also ask for a credit-card
guarantee, especially on weekends, and the card step uses a hosted payment iframe.
→ `OntopoAdapter.booking_requirements()` declares `otp_required=True`, so `auto_order`
falls back to sending deep links.

## Tabit (tabitisrael.co.il, API on tgm-api.tabit.cloud)

| Item | Assumption | Config key |
|---|---|---|
| Venue id | Mongo-style `orgId` in `https://tabitisrael.co.il/online-reservations/create-reservation?...&orgId=<id>` | `organization_id` |
| Availability | `POST https://tgm-api.tabit.cloud/rsv/booking/temp-reservations` | `base_url` + `availability_endpoint` |
| Request body | `{"organization": "<orgId>", "type": "future_reservation", "standby_reservation": false, "seats_count": 2, "reserved_from": "<UTC ISO-8601>", "preference": "<inside/outside/bar…, optional>"}` | built in code / `preference` |
| Exact match | `2xx {"reservation": {"_id", "reserved_from", "preference", ...}}`. This is a **temporary hold**. | parser |
| No exact match | Often `409 {"alternative_results": [{"reserved_from"\|"time", "preference"\|"area"}]}`. It may also come keyed by area. | parser |
| Release hold | `DELETE /rsv/booking/temp-reservations/<_id>`, best effort. Holds also expire on their own after a few minutes. | `release_endpoint` |
| Deep link | `https://tabitisrael.co.il/online-reservations/create-reservation?step=search&orgId=<id>&date=YYYY-MM-DD&time=HH:MM&seats=N` | `deep_link_template` |

Times are UTC on the wire. The adapter converts them from and to `Asia/Jerusalem`.

**Side effect to be aware of.** Tabit's public search seems to work by *creating a
temporary hold*. The adapter releases every hold it receives right away and keeps the
number of probes low: two query times per evening and about 14 evenings, so about 28
requests per day. If DevTools shows a read-only availability endpoint (for example a
`.../availability` GET), switch `availability_endpoint` to it and adapt
`TabitAdapter.check_day`.

**Booking feasibility.** Completing a Tabit reservation asks for contact details and
confirms the phone number with an SMS code. Some venues add a card guarantee through a
hosted payment page. → `otp_required=True`, so `auto_order` falls back to deep links.

## Anti-blocking safeguards (implemented in `searcher.HttpClient`)

* Realistic browser headers: Chrome User-Agent, `Accept-Language: he-IL…`, `Origin` and `Referer`.
* At least 2.5 s plus 0–1.5 s random jitter between requests (`http.min_interval_seconds`, `jitter_seconds`).
* Retries with exponential backoff on network errors, 5xx and 429. `Retry-After` is honoured, capped at 60 s.
* 401, 403, 429-after-retries and captcha or Cloudflare HTML pages all count as `blocked`.
  The platform is then **skipped for the rest of the run**, which stops the run from
  hammering it. The run itself still finishes.
* One run per day, about 14 evenings × 2 query times per platform.

## Conclusion

| | Ontopo | Tabit |
|---|---|---|
| Availability without login | Yes (anonymous token) *(unverified)* | Yes (temp-hold probe) *(unverified)* |
| SMS OTP to book | Yes | Yes |
| Card guarantee | Venue-dependent | Venue-dependent |
| Auto-booking feasible unattended | **No** → notify + deep link | **No** → notify + deep link |

So the default `mode: "notify_only"` is the realistic setup. The `auto_order` code path
exists, and it's tested with a fake adapter. It only books on a platform whose adapter
declares that it needs no OTP and no card.

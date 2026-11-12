# Travel deals section (#52, #53)

`src/price_watcher/travel_deals.py` builds the "Travel deals" section of the
daily email from a JSON feed. It filters and groups deals; it does not fetch
them.

## Rules

| Rule | Value | Setting |
|---|---|---|
| Hotel stars | 4 to 5 inclusive (4.5 passes, 3.5 fails, missing fails) | fixed |
| Package with no 4+ star hotel | dropped, never shown with a lower-tier hotel | fixed |
| Flight-only deal | kept (it has no hotel to filter) | fixed |
| Europe window | today to today + 50 days, inclusive | `PW_TRAVEL_EUROPE_DAYS` |
| Europe count | up to 10, cheapest first | `PW_TRAVEL_EUROPE_LIMIT` |
| Other continents window | today to today + 365 days, inclusive | `PW_TRAVEL_OTHER_DAYS` |
| Other continents count | 4 each, cheapest first | `PW_TRAVEL_OTHER_LIMIT` |
| Asia | the cheapest Japan deal and the cheapest Thailand deal are always included; a missing one is noted in the email | fixed |

A continent with no qualifying deal shows a one-line note instead of a table.

## Feed format

`<app data>/travel_deals.json`, or the path in `PW_TRAVEL_DEALS_FILE`:

```json
{"deals": [
  {"destination": "Tokyo", "country": "Japan",
   "depart_date": "2026-12-01", "return_date": "2026-12-08",
   "price": 2890, "currency": "ILS", "url": "https://...",
   "hotels": [{"name": "Hotel X", "stars": 4.5}]}
]}
```

- `country` may be an English name, an ISO-2 code or (for Japan and
  Thailand) a Hebrew name. `continent` can be given to override the lookup.
- Leave out `hotels` for a flight-only deal. A single `hotel` object also works.
- Entries without a valid `depart_date` or a positive `price` are ignored.

## Open questions

- **Data source.** No travel-deal site is scraped yet. A source (and its
  terms of use) needs to be chosen before the section has real content.
- **Europe baseline.** The issue asks to keep "the current row count" for
  Europe, but there was no earlier travel section to count. The default is 10;
  change `PW_TRAVEL_EUROPE_LIMIT` to the number you want.

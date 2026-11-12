# Price Watcher

A local CLI that does two things once a day:

- **Price check** — fetches every watched product URL, extracts the current price, and emails you when the price, stock status or promotion changes.
- **Daily digest** — one email with site-wide promo codes on the shops you watch, the cheapest creatine on iHerb, The Sims 4 pack discounts at EA, meat deals from Israeli supermarket price feeds, vacation deals and, when configured, Facebook Marketplace deals.

Runs on macOS and Windows. No cloud service; the tests run in GitHub Actions on Ubuntu, macOS and Windows (`.github/workflows/price-watcher-tests.yml`). Emails are written in Hebrew.

Flight watches for El Al and Arkia (`add-flight`, `check-flights`) are a separate, still unverified feature; see `docs/flights.md`.

## Install

### macOS
```bash
cd price-watcher
./setup.sh
```

### Windows (PowerShell)
```powershell
cd price-watcher
.\setup.ps1
```

Both scripts install [`uv`](https://github.com/astral-sh/uv) if missing, create a Python 3.11+ virtual environment, install dependencies, and install the Playwright Chromium browser.

Chromium is needed for pages that only render prices in JavaScript, for shops that block plain requests, and for the iHerb creatine scan. If you set the environment up by hand, run `python -m playwright install chromium` once; without it those watches fail with "Playwright Chromium browser not available" in the log.

## Configure

1. Copy `.env.example` → `.env` (the setup script does this for you if `.env` is missing).
2. Edit `.env`:
   - `PW_EMAIL_TO` — the address that should receive the emails.
   - `PW_SMTP_*` — Gmail works out of the box. You need an **app password**, not your normal password. Port 587 uses STARTTLS; port 465 uses implicit TLS.
   - `PW_FAILURE_THRESHOLD`, `PW_SANITY_JUMP_PCT`, `PW_DELAY_*` — check behaviour, see "How changes are detected".
   - `PW_MEAT_CITIES`, `PW_STORES_PER_REGION`, `PW_TOP_PER_CATEGORY`, `PW_BAREKET_SOURCE` — the supermarket scan: which cities (empty = the cities marked `ON` in `_REGIONS` in `supermarkets.py`, now Herzliya and Ramat HaSharon; a comma-separated list such as `הרצליה,רמת השרון,תל אביב` or `herzliya,raanana,kfar-saba`, the regions `sharon`, `tel-aviv`, `rishon`, or `הכל`/`all`), a cap on stores per chain and region (0 = all), how many deals each category table shows (default 5), and where Super Bareket's files are read from (default `bina:superbareket`; also `publishedprices:<username>` or `off`). The older names `PW_MEAT_REGIONS` and `PW_MEAT_TOP_PER_CATEGORY` are still read when the new ones are not set; `PW_TOP_PER_REGION` is no longer used.
   - `PW_DEALS_*` — the vacation deals, see "Daily report and vacation deals".
   - `PW_FB_*` — the Facebook Marketplace section; off while `PW_FB_QUERIES` is empty. See `docs/facebook-marketplace.md`.
   - `PW_LOG_LEVEL` — `DEBUG`, `INFO`, `WARNING` or `ERROR`. The `--log-level` option overrides it for one run.

A missing or malformed setting is reported as a one-line `Error: …` and the command exits with a non-zero code.

### Creating a Gmail app password
1. Turn on 2-Step Verification: https://myaccount.google.com/security
2. Create an app password: https://myaccount.google.com/apppasswords (select "Mail", give it a label like "Price Watcher")
3. Paste the 16-character password into `PW_SMTP_PASSWORD` in `.env`.
4. Use the same Gmail address for `PW_SMTP_USER` and `PW_EMAIL_FROM`.

### Verify the setup
```
python -m price_watcher test-email
```
You should receive a sample email within a few seconds.

## Usage

```
python -m price_watcher add <url> [--name NAME] [--selector CSS] [--only-drops] [--min-change 5%|10] [--target PRICE] [--no-promo] [--excluded-from-promo]
python -m price_watcher set <url-or-id> [options]    # change a watch's settings (see below)
python -m price_watcher list
python -m price_watcher remove <url-or-id>
python -m price_watcher check [--dry-run]            # price check for every watched URL
python -m price_watcher digest [--dry-run]           # build and send the daily digest
python -m price_watcher test-email
python -m price_watcher install-schedule             # daily price check at 09:00
python -m price_watcher uninstall-schedule
python -m price_watcher install-digest-schedule      # daily digest at 09:00
python -m price_watcher uninstall-digest-schedule
python -m price_watcher status                       # both schedules + the last check run
python -m price_watcher version
python -m price_watcher marketplace-preview          # run the Marketplace scan, print it, send nothing
python -m price_watcher add-flight --airline elal|arkia --from TLV --to ATH --date YYYY-MM-DD   # see docs/flights.md
python -m price_watcher list-flights | remove-flight <id> | check-flights [--dry-run]
```

`check --dry-run` runs the full pipeline (fetch, extract, compare) but does not send email or change stored prices. It prints, per link, the price and either `would not notify` or `would send: <subject>`, followed by the first lines of the email that would be sent. It still saves the fetched pages under `raw-responses/` in the data folder and writes the log, and it does not need the SMTP settings. The run is recorded in `last-run.json` marked as a dry run; `status` still shows the last real run next to it. `digest --dry-run` collects and prints every section without emailing.

### Per-link settings

Set them when adding (`add <url> --only-drops ...`) or later with `set <url-or-id> ...`:

| Option | Effect |
|---|---|
| `--only-drops` (`set`: `--only-drops` / `--all-changes`) | Only email when the price goes down. |
| `--min-change 5%` or `--min-change 10` | Ignore price changes smaller than 5% / 10 (in the item's currency), measured against the price you were last told about, so small steps add up. `set ... --min-change none` removes it. |
| `--target 79.90` | Only email about a price change once the price is at or below 79.90. `set ... --target none` removes it. |
| `--no-promo` (`set`: `--promo` / `--no-promo`) | Do not read the shop's promotion banner (MyProtein) for this link. |
| `--excluded-from-promo` (`set`: `--excluded-from-promo` / `--not-excluded-from-promo`) | The item is excluded from the shop's promotions: no estimated final price; the email says so. |
| `set ... --pause` / `--resume` | Skip the link in `check` without removing it. |
| `set ... --name` / `--selector` | Rename the watch or change its CSS selector. |

These filters only hold back price-change emails; stock and promotion changes are still reported, and every observed price is still saved in the price history.

### MyProtein promotions

For MyProtein links the watcher reads the site's promotion banner (percentage, code, and stated conditions such as a minimum spend, "selected items" or an end date) and stores an *estimated* final price (`displayed price × (1 − discount)`, marked "(הערכה)" in emails). Emails say whether the displayed price, the promotion, or both changed. A promotion that ends is reported as "המבצע הסתיים", not as a price rise. A tiered banner ("up to 70%") or a code without a readable percentage is shown as "displayed price only" and never triggers an email by itself.

### Exit codes

`check` exits with **0** when every link was read and any change email was sent, **1** when at least one link could not be fetched or its price could not be read, **2** on a configuration error, and **3** when a change email could not be sent (SMTP failure). In the last case the detected changes are *not* saved, so the next run detects them again and retries the email. `digest` exits with 1 when its email could not be sent. These codes are what the OS scheduler records as the task's last result ("Last Run Result" in Task Scheduler, `launchctl print` on macOS), and `status` shows the last exit code and whether the email failed.

When you `add` a URL, tracking parameters (`utm_*`, `gclid`, `fbclid`, `ref`, …) are removed and the host is lower-cased; every other query parameter is kept because it may identify the product or variant. Percent-encoded Hebrew paths are stored readable, so the encoded and the readable form of a link count as the same watch. Only `http://` and `https://` links are accepted; adding a link that is already watched exits with 1.

## How changes are detected

- **What triggers an email:** a price change, a stock change, a promotion that appears, changes or ends, a watch that stops working, and a watch that works again. Changes that happen together arrive in one email.
- **If the email cannot be sent**, the change is not recorded, so the next run detects and emails it again.
- **Large jumps** (more than `PW_SANITY_JUMP_PCT` percent) are held back for one run and reported if the next run confirms them.
- **A price of 0** counts as a failed check, not as a price.
- **A product page that now redirects to the home page** counts as "page not found" (the product was probably removed).
- **A currency switch** is held back; after `PW_FAILURE_THRESHOLD` consecutive runs in the new currency it is accepted and reported as a change showing both currencies.
- **Shopify products:** a URL with `?variant=<id>` tracks exactly that variant; if the variant no longer exists the watch fails rather than following another one. Without `?variant=`, the product's first variant is tracked.
- **Structured data:** when a page lists several variants and the URL names one (`?variation=`, `?variant=`, `?sku=`), that variant's price is used; otherwise the lowest listed price.

## Daily digest sections

| Section | Source | Notes |
|---|---|---|
| Site-wide promos | Home page of every shop in the watch list | A code is only paired with the percentage of the same offer. Category-only offers are not listed. |
| Creatine | iHerb search (Israel store) | iHerb usually blocks plain requests, so this section needs Chromium. Powders only; first results page only. |
| The Sims 4 | EA "all packs" store page | Only the first 50 packs the page serves are read; the email says so. |
| Supermarket meat deals | Government-mandated price feeds: Shufersal, the chains on url.publishedprices.co.il, and Super Bareket (superbareket.binaprojects.com) | Stores in the enabled cities only — Herzliya and Ramat HaSharon by default (`PW_MEAT_CITIES`, or the `ON`/`OFF` column of `_REGIONS` in `supermarkets.py`). One table per category — one cut of one animal, fresh and frozen apart (e.g. "כנפיים עוף", "לשון בקר – קפוא") — with the `PW_TOP_PER_CATEGORY` cheapest deals (default 5), ranked by promo price per kg; each row shows the chain and its branches. Packaged items are converted by pack weight; deals whose weight is unknown, and catch-all groups such as "בשר – אחר", are counted but not ranked. A chain with no branch in the cities is listed as such. Each deal shows its end date. |
| Vacation deals | Issta, Eshet Tours, Flying Carpet, Gulliver, Hulyo, Lastminute, Travelist, Secret Flights | See "Daily report and vacation deals". |
| Facebook Marketplace | Marketplace search pages for `PW_FB_QUERIES` | Off (no section) while `PW_FB_QUERIES` is empty. Reports new listings and price drops of at least `PW_FB_MIN_DROP_ILS` ₪ and `PW_FB_MIN_DROP_PCT` %. Facebook often shows logged-out visitors a login wall; that is reported inside the section. Unverified against the live site; see `docs/facebook-marketplace.md`. |

If a section's scan fails, the email says which one and why, and the subject line names the failed sections.

### Daily report and vacation deals

```
python -m price_watcher digest [--dry-run] [--test]   # full daily report
python -m price_watcher deals  [--dry-run] [--test]   # vacation deals only
python -m price_watcher install-digest-schedule       # run `digest` daily at 09:00
```

The daily report (`digest`) goes to `PW_EMAIL_TO` and ends with a vacation-deals section: the cheapest packages and flights from Issta, Eshet Tours, Flying Carpet, Gulliver, Hulyo, Lastminute, Travelist and Secret Flights (`src/price_watcher/vacations.py`).

Addresses in `PW_DEALS_EMAIL_TO` (comma-separated) get a separate email with **only** the vacation deals each time `digest` runs. They never get the rest of the report.

`--test` is for trying things out: `digest --test` emails only `PW_EMAIL_TO` and saves nothing; `deals --test` sends the deals-only email to `PW_EMAIL_TO` instead of the deals-only recipients.

## Known limits

- Checks only happen while the computer is on and awake. A closed laptop means no checks until it's opened again. On Windows the scheduled tasks are set to run on battery power as well.
- Sites that require login are out of scope.
- Flight watches (El Al, Arkia) are scaffolded but their site adapters are unverified, and no schedule is installed for `check-flights`; see `docs/flights.md`.

## Troubleshooting

Logs: `~/Library/Logs/price-watcher/price-watcher.log` (macOS) or `%LOCALAPPDATA%\Logs\price-watcher\price-watcher.log` (Windows).

### "Price not found"
The extractor tried, in order: Shopify product JSON → JSON-LD → Open Graph meta tags → microdata → your CSS selector → the same steps again on the page rendered in headless Chromium. None gave a price. Options:
- Add `--selector "<css>"` on `add` (remove and re-add the URL) pointing at the element with the price.
- Make sure Chromium is installed (see Install).
- Check the log and the saved page under `raw-responses/`.

### "Blocked" / 403 / 429 / CAPTCHA
Some shops reject bot-like requests. For a blocked page the watcher tries the Shopify product JSON (when the URL looks like a Shopify product) and then headless Chromium. On HTTP 429 it waits once, for at most 15 seconds, and otherwise moves on; it does not keep waiting on a host that is rate-limiting. If the same URL fails consistently there is no good workaround without a residential proxy.

### Email not arriving
- Run `python -m price_watcher status`. If it says the change email could NOT be sent, the scheduled `check` also exited with code 3; the changes are kept and retried on the next run.
- Run `python -m price_watcher test-email`; it prints "Test email sent." or a one-line error and exits with 1 on failure.
- Gmail requires an app password (16 chars, no spaces). Your normal account password will fail with an authentication error.
- Check spam. Gmail sometimes files unknown-sender mail as spam initially; mark the first one "not spam" to fix.

### "Watch stopped working" email
Sent once, on the `PW_FAILURE_THRESHOLD`-th consecutive failed check (6 by default), also for a link that never worked. The watch is still checked every day; when it works again you get a "working again" email that includes any price change that happened in the meantime. The alert says why and what to do: blocked by the site (with the HTTP status), page not found (404/410, or the link now redirects to the home page — the product was probably removed), the size/colour in the link no longer exists (remove the watch and add it again with an existing variant), or the price could not be read / was 0 (re-add with `--selector`). `status` and `last-run.json` show the same details for the last run.

### A price that looks wrong
A price of 0 is treated as an unreadable price, not a change. A currency change, or a jump bigger than `PW_SANITY_JUMP_PCT` (default 90%), is held back before any email is sent (see "How changes are detected").

### Corrupt data files
If `watchlist.json` or `state.json` in the data folder cannot be read, the watcher keeps working, logs an error and saves a copy next to it as `*.corrupt-<date>` so nothing is lost silently. A single bad entry in the watch list is skipped (and logged) instead of stopping `check`, `list` and `digest`.

## Development

```
pip install -e ".[test]"     # ".[dev]" installs the same
pytest
```

CI runs exactly this (`python -m pip install -e ".[test]"` then `python -m pytest -q`) on Ubuntu, macOS and Windows, plus the `restaurant-orderer` tests. The tests run offline against saved page fragments in `tests/fixtures/` and use a temporary data folder; they never send email.

# Price Watcher

A local CLI that does two things once a day:

- **Price check** — fetches every watched product URL, extracts the current price, and emails you when the price, stock status or promotion changes.
- **Daily digest** — one email with site-wide promo codes on the shops you watch, the cheapest creatine on iHerb, The Sims 4 pack discounts at EA, and meat deals from Israeli supermarket price feeds.

Runs on macOS and Windows. No cloud, no CI. Emails are written in Hebrew.

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
   - `PW_MEAT_CITIES`, `PW_STORES_PER_REGION`, `PW_TOP_PER_CATEGORY` — the supermarket scan: which cities (empty = the cities marked `ON` in `_REGIONS` in `supermarkets.py`, now Herzliya and Ramat HaSharon; a comma-separated list such as `הרצליה,רמת השרון,תל אביב`, or `הכל`), a cap on stores per chain and region (0 = all), and how many deals each category table shows (default 5).
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
python -m price_watcher add <url> [--name NAME] [--selector CSS]
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
```

`check --dry-run` runs the full pipeline but does not send email or change stored prices; it prints what *would* have happened per link. It still saves the fetched pages under `raw-responses/` in the data folder and writes the log, and it does not need the SMTP settings. `digest --dry-run` collects and prints every section without emailing.

`check` exits with 1 when at least one watch failed and `digest` exits with 1 when the email could not be sent, so the OS scheduler shows a failed run.

When you `add` a URL, tracking parameters (`utm_*`, `gclid`, `fbclid`, `ref`, …) are removed and the host is lower-cased; every other query parameter is kept because it may identify the product or variant.

## How changes are detected

- **What triggers an email:** a price change, a stock change, a promotion that appears, changes or ends, a watch that stops working, and a watch that works again. Changes that happen together arrive in one email.
- **If the email cannot be sent**, the change is not recorded, so the next run detects and emails it again.
- **Large jumps** (more than `PW_SANITY_JUMP_PCT` percent) are held back for one run and reported if the next run confirms them.
- **A price of 0** counts as a failed check, not as a price.
- **A currency switch** is held back; after `PW_FAILURE_THRESHOLD` consecutive runs in the new currency it is accepted and reported as a change showing both currencies.
- **Shopify products:** a URL with `?variant=<id>` tracks exactly that variant; if the variant no longer exists the watch fails rather than following another one. Without `?variant=`, the product's first variant is tracked.
- **Structured data:** when a page lists several variants and the URL names one (`?variation=`, `?variant=`, `?sku=`), that variant's price is used; otherwise the lowest listed price.

## Daily digest sections

| Section | Source | Notes |
|---|---|---|
| Site-wide promos | Home page of every shop in the watch list | A code is only paired with the percentage of the same offer. Category-only offers are not listed. |
| Creatine | iHerb search (Israel store) | iHerb usually blocks plain requests, so this section needs Chromium. Powders only; first results page only. |
| The Sims 4 | EA "all packs" store page | Only the first 50 packs the page serves are read; the email says so. |
| Supermarket meat deals | Government-mandated price feeds: Shufersal, the chains on url.publishedprices.co.il, and Super Bareket (superbareket.binaprojects.com) | Stores in the enabled cities only — Herzliya and Ramat HaSharon by default (`PW_MEAT_CITIES`, or the `ON`/`OFF` column of `_REGIONS` in `supermarkets.py`). One table per category — one cut of one animal, fresh and frozen apart (e.g. "כנפיים עוף", "לשון בקר – קפוא") — with the `PW_TOP_PER_CATEGORY` cheapest deals (default 5), ranked by promo price per kg; each row shows the chain and its branches. Packaged items are converted by pack weight; deals whose weight is unknown, and catch-all groups such as "בשר – אחר", are counted but not ranked. A chain with no branch in the cities is listed as such. |

If a section's scan fails, the email says which one and why, and the subject line names the failed sections.

## Known limits

- Checks only happen while the computer is on and awake. A closed laptop means no checks until it's opened again. On Windows the scheduled tasks are set to run on battery power as well.
- Sites that require login are out of scope.
- Flight prices (El Al, Arkia) are a separate, unbuilt feature.
- Per-watch filters (`notify_only_on_drop`, `min_change_pct`, `paused`) exist but can only be set by editing `watchlist.json` in the data folder.

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
- Run `python -m price_watcher test-email`; a failure is printed as a one-line error.
- Gmail requires an app password (16 chars, no spaces). Your normal account password will fail with an authentication error.
- Check spam. Gmail sometimes files unknown-sender mail as spam initially; mark the first one "not spam" to fix.

### "Watch stopped working" email
Sent once, on the `PW_FAILURE_THRESHOLD`-th consecutive failed check (6 by default). The watch is still checked every day; when it works again you get a "working again" email that includes any price change that happened in the meantime. If a site has permanently changed, `remove` and `add` it again — or add a `--selector`.

## Development

```
pip install -e ".[dev]"
pytest
```

The tests run offline against saved page fragments in `tests/fixtures/` and use a temporary data folder; they never send email.

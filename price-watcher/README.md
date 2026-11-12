# Price Watcher

A local CLI that watches product URLs, extracts the current price once a day (09:00), and emails you when it changes. Runs on macOS and Windows. No cloud, no CI.

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

Both scripts install [`uv`](https://github.com/astral-sh/uv) if missing, create a Python 3.11+ virtual environment, install dependencies, and install the Playwright Chromium browser (used for pages that only render prices in JavaScript).

## Configure

1. Copy `.env.example` → `.env` (the setup script does this for you if `.env` is missing).
2. Edit `.env`:
   - `PW_EMAIL_TO` — the address that should receive price-change emails.
   - `PW_SMTP_*` — Gmail works out of the box. You need an **app password**, not your normal password.

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
python -m price_watcher list
python -m price_watcher remove <url-or-id>
python -m price_watcher set <url-or-id> [options]   # change a watch's settings (see below)
python -m price_watcher check [--dry-run]
python -m price_watcher test-email
python -m price_watcher install-schedule     # install daily (09:00) OS schedule
python -m price_watcher uninstall-schedule
python -m price_watcher status               # when was the last run, is the schedule on
python -m price_watcher digest [--dry-run]   # the daily report email (site promos, creatine, Sims 4, meat)
```

`--dry-run` runs the full pipeline (fetch, extract, compare) but does not send email and does not change the state file. It prints, per link, the previous and current price and either `would not notify` or `would send: <subject>`, followed by the first lines of the email that would be sent. The run is recorded in `last-run.json` marked as a dry run; `status` still shows the last real run next to it. Dry runs do not need the SMTP settings.

### Per-link settings

Set them when adding (`add <url> --only-drops ...`) or later with `set <url-or-id> ...`:

| Option | Effect |
|---|---|
| `--only-drops` (`set`: `--only-drops` / `--all-changes`) | Only email when the price goes down. |
| `--min-change 5%` or `--min-change 10` | Ignore changes smaller than 5% / 10 (in the item's currency). Measured against the price you were last told about, so small steps add up. `set ... --min-change none` removes it. |
| `--target 79.90` | Only email about a price change once the price is at or below 79.90. `set ... --target none` removes it. |
| `--no-promo` (`set`: `--promo` / `--no-promo`) | Do not read the shop's promotion banner (MyProtein) for this link. |
| `--excluded-from-promo` | The item is excluded from the shop's promotions: no estimated final price; the email says so. |
| `set ... --pause` / `--resume` | Skip the link in `check` without removing it. |

Every observed price is still saved in the price history, even when a setting holds the email back.

### MyProtein promotions

For MyProtein links the watcher reads the site's promotion banner (percentage, code, and stated conditions such as a minimum spend, "selected items" or an end date) and stores an *estimated* final price (`displayed price × (1 − discount)`). Emails say e.g. `מוצג 189 ₪, בערך 103.95 ₪ עם הקוד X45 (45%) (הערכה)`, and say whether the displayed price, the promotion, or both changed. A promotion that ends is reported as "המבצע הסתיים", not as a price rise. A tiered banner ("up to 70%") or a code without a readable percentage is shown as "displayed price only" and never triggers an email by itself.

### Exit codes

`check` exits with **0** when every link was read and any change email was sent, **1** when at least one link could not be fetched or its price could not be read, and **3** when a change email could not be sent (SMTP failure or missing SMTP settings). In the last case the detected changes are *not* saved, so the next run detects them again and retries the email. `digest` exits with 1 when its email could not be sent. These codes are what the OS scheduler records as the task's last result ("Last Run Result" in Task Scheduler, `launchctl print` on macOS), and `status` shows the last exit code and whether the email failed.

### Supermarket meat deals

The daily email includes meat deals from the supermarkets' public price files (Israeli price-transparency law): Shufersal, the ten chains on url.publishedprices.co.il, and Bareket (ברקת).

- **Area.** Only stores in **Herzliya and Ramat HaSharon** are scanned for now. To scan other cities set `PW_MEAT_REGIONS` in `.env`. It takes city or region names, e.g. `sharon,tel-aviv,rishon` for the previous scope, or `all`.
- **Categories.** Each category is one cut of one animal (`חזה הודו`, `צלעות טלה`, `כנפיים עוף` …). Frozen items get their own table (`לשון בקר · קפוא`).
- **Ranking.** Each category shows the `PW_MEAT_TOP_PER_CATEGORY` (default 5) cheapest deals by promo price **per kg**, with chain, store and end date. Items whose pack weight is unknown come after the per-kg rows. The catch-alls (`עוף – אחר`, `בשר – אחר`, `אחר`) mix unrelated cuts, so they are listed at the end without ranking.
- **Bareket** is read from its Bina Projects portal. If that changes, set `PW_BAREKET_SOURCE` (`bina:<url>`, `publishedprices:<username>` or `off`).
- `PW_TOP_PER_REGION` is no longer used.

## Travel deals

The daily email can include a "Travel deals" section grouped by continent.
Only 4- and 5-star hotels are shown (4.5 passes; 3.5, unrated and missing
ratings do not), and a package with no such hotel is dropped. Europe lists up
to `PW_TRAVEL_EUROPE_LIMIT` deals departing in the next 50 days. Every other
continent lists 4 deals departing in the next 365 days, and Asia always
includes Japan and Thailand when either has a deal. The section reads
`travel_deals.json`; there is no scraper for it yet. See
[docs/travel-deals.md](docs/travel-deals.md).

## Known limits

- Checks only happen while the computer is on and awake. A closed laptop means no checks until it's opened again.
- Sites that require login are out of scope.
- Flight prices (El Al, Arkia) are a separate, unbuilt feature.

## Troubleshooting

### "Price not found"
The extractor tried JSON-LD → Open Graph → microdata → CSS selector and none matched. Options:
- Add `--selector "<css>"` on `add` (or re-add the URL) pointing at the element with the price.
- Open the page in a browser and view the source; if the price is only visible after JavaScript runs, the extractor will fall back to Playwright automatically on the next check.
- Check `~/Library/Logs/price-watcher/price-watcher.log` (macOS) or `%LOCALAPPDATA%\Logs\price-watcher\price-watcher.log` (Windows) for details.

### "Blocked" / 403 / CAPTCHA
Some shops reject bot-like requests. The extractor will automatically fall back to a headless browser, but a few sites block that too. If the same URL fails consistently, there's no good workaround without a residential proxy.

### Email not arriving
- Run `python -m price_watcher status`. If it says the change email could NOT be sent, the scheduled `check` also exited with code 3; the changes are kept and retried on the next run.
- Run `python -m price_watcher test-email`; it prints the SMTP error and exits with 1 on failure.
- Gmail requires an app password (16 chars, no spaces). Your normal account password will fail with an authentication error.
- Check spam. Gmail sometimes files unknown-sender mail as spam initially; mark the first one "not spam" to fix.

### "Watch stopped working" email
After `PW_FAILURE_THRESHOLD` (default 6) consecutive failures the watcher sends one email and stops emailing about it until the link works again, when it sends one "working again" email. The alert says why: blocked by the site (with the HTTP status), page not found (404, or the link now redirects to the home page — the product was probably removed), the size/colour in the link no longer exists (remove the watch and add it again with an existing variant), or the price could not be read / was 0 (re-add with `--selector`). `status` and `last-run.json` show the same details for the last run.

### A price that looks wrong
A price of 0 is treated as an unreadable price, not a change. A currency change, or a jump bigger than `PW_SANITY_JUMP_PCT` (default 90%), is checked again on the next run before any email is sent.

### Corrupt data files
If `watchlist.json` or `state.json` in the data folder cannot be read, the watcher keeps working, logs an error and saves a copy next to it as `*.corrupt-<date>` so nothing is lost silently. A single bad entry in the watch list is skipped (and logged) instead of stopping `check`, `list` and `digest`.

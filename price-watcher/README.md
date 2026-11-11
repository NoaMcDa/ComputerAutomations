# Price Watcher

A local CLI that watches product URLs, extracts the current price once an hour, and emails you when it changes. Runs on macOS and Windows. No cloud, no CI.

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
python -m price_watcher add <url> [--name NAME] [--selector CSS]
python -m price_watcher list
python -m price_watcher remove <url-or-id>
python -m price_watcher check [--dry-run]
python -m price_watcher install-schedule     # install hourly OS schedule
python -m price_watcher uninstall-schedule
python -m price_watcher status               # when was the last run, is the schedule on
```

`--dry-run` runs the full pipeline but does not send email or write state; it prints what *would* have happened per link. Use it to sanity-check extraction against the real sites without noise.

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
- Run `python -m price_watcher test-email` and watch the log output.
- Gmail requires an app password (16 chars, no spaces). Your normal account password will fail with an authentication error.
- Check spam. Gmail sometimes files unknown-sender mail as spam initially; mark the first one "not spam" to fix.

### "Watch stopped working" email
After 6 consecutive failures the watcher sends one email and stops retrying every hour until the site recovers. If a site has permanently changed, `remove` and `add` it again — or add a `--selector`.

# Audit fixture for the daily email

Invented data pushed through the real pipeline, used by
`docs/daily-email-qa-ux-audit.md`. Nothing here is real pricing, and nothing
is sent.

    python docs/audit-fixture/render.py . /tmp/audit-out     # from price-watcher/
    node docs/audit-fixture/measure.js /tmp/audit-out        # needs Playwright + Chromium

`render.py` writes `email.html`, `email.txt`, `subject.txt`, `stats.json`,
`meat_dump.txt` and `vac_dump.txt`. `measure.js` prints the page width, height
and row sizes at 360, 375 and 800 px. Vacation dates are relative to today, so
the vacation rows change from day to day; the meat fixture does not.

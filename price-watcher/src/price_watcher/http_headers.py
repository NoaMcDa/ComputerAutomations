"""Shared HTTP identity for every scraper.

One place for the browser User-Agent so the modules stop drifting apart
(they used to carry Chrome 120 and Chrome 131 strings side by side). Bump the
major version here every few months; Chrome's reduced UA format keeps the
minor/build parts at ``0.0.0``.
"""

from __future__ import annotations

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

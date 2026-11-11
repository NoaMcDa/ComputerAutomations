"""Site-specific extractor plugins.

Add a new plugin by creating a module in this folder that exposes a
`SiteExtractor` instance, then register it in `REGISTRY` below.
"""

from __future__ import annotations

from typing import List

from price_watcher.contracts import SiteExtractor


def _build_registry() -> List[SiteExtractor]:
    registry: List[SiteExtractor] = []
    try:
        from price_watcher.sites.myprotein import extractor as myprotein_extractor
        registry.append(myprotein_extractor)
    except ImportError:
        pass
    return registry


REGISTRY: List[SiteExtractor] = _build_registry()


def for_url(url: str) -> SiteExtractor | None:
    for ext in REGISTRY:
        if ext.applies_to(url):
            return ext
    return None

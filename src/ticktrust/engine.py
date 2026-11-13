"""Reference price: median of fresh sources, outliers removed, quorum enforced.

This is the same family of method exchanges use for index and mark prices: no single
venue can move the reference, stale data is never used, and when too few sources are
left the service says so instead of guessing.
"""

from __future__ import annotations

from statistics import median

from .models import Quote, RefPrice, Status
from .policy import Policy


def reference(
    pair: str,
    quotes: list[Quote],
    now: float,
    policy: Policy,
    excluded: dict[str, str] | None = None,
) -> RefPrice:
    """quotes: latest quote per source. excluded: sources the monitor already distrusts."""
    dropped: dict[str, str] = dict(excluded or {})
    fresh: dict[str, float] = {}
    for q in quotes:
        if q.source in dropped:
            continue
        if q.price <= 0:
            dropped[q.source] = "invalid price"
        elif now - q.ts > policy.max_age_s:
            dropped[q.source] = f"stale ({now - q.ts:.0f}s old)"
        else:
            fresh[q.source] = q.price

    used = dict(fresh)
    if len(used) >= 2:
        m = median(used.values())
        for s, p in list(used.items()):
            dev = abs(p / m - 1) * 100
            if dev > policy.max_dev_pct:
                dropped[s] = f"outlier ({dev:.2f}% from median)"
                del used[s]
        # Two sources that disagree: nobody to break the tie, trust neither
        if len(fresh) == 2 and len(used) < 2:
            used = {}

    if not used:
        return RefPrice(pair, None, None, None, Status.HALT, now, [], dropped)
    price = median(used.values())
    status = Status.OK if len(used) >= policy.min_sources else Status.DEGRADED
    return RefPrice(
        pair, price, min(used.values()), max(used.values()), status, now, sorted(used), dropped
    )

"""Keeps the latest state of every feed, computes reference prices and opens/closes incidents."""

from __future__ import annotations

from collections.abc import Callable

from .engine import reference
from .models import Incident, Kind, Quote, RefPrice, Status
from .policy import Policy

AlertFn = Callable[[str, Incident], None]  # ("open" | "resolved", incident)


class Monitor:
    def __init__(self, policy: Policy | None = None, alert: AlertFn | None = None):
        self.policy = policy or Policy()
        self.alert = alert
        self.last: dict[str, dict[str, Quote]] = {}  # pair -> source -> latest quote
        self.responded: dict[str, dict[str, float]] = {}  # pair -> source -> last answer time
        self.changed: dict[str, dict[str, tuple[float, float | None]]] = {}  # (time, ref then)
        self.refs: dict[str, RefPrice] = {}
        self.incidents: list[Incident] = []
        self._open: dict[tuple, Incident] = {}
        self._depeg_since: dict[str, float] = {}

    # -- feeding -----------------------------------------------------------------------
    def ingest(self, pair: str, quotes: list[Quote], now: float) -> RefPrice:
        """quotes: what each source returned this round (missing source = no answer)."""
        p = self.policy
        last = self.last.setdefault(pair, {})
        responded = self.responded.setdefault(pair, {})
        changed = self.changed.setdefault(pair, {})
        prev_ref = self.refs.get(pair)
        prev_price = prev_ref.price if prev_ref else None

        for q in quotes:
            old = last.get(q.source)
            if old is None or old.price != q.price:
                changed[q.source] = (now, prev_price if prev_price else q.price)
            last[q.source] = q
            responded[q.source] = now

        frozen: dict[str, str] = {}
        for s, (t, ref_then) in changed.items():
            if now - t >= p.frozen_after_s and ref_then and prev_price:
                move = abs(prev_price / ref_then - 1) * 100
                if move >= p.frozen_market_move_pct:
                    frozen[s] = f"frozen ({now - t:.0f}s unchanged, market moved {move:.2f}%)"

        ref = reference(pair, list(last.values()), now, p, excluded=frozen)
        self.refs[pair] = ref

        # -- incidents -----------------------------------------------------------------
        for s in last:
            outage = now - responded.get(s, now) >= p.outage_after_s
            why = ref.dropped.get(s, "")
            self._set(Kind.OUTAGE, pair, s, outage, now, f"no answer for {now - responded[s]:.0f}s")
            answered_now = responded.get(s) == now  # silent sources are outages, not stale
            self._set(Kind.STALE, pair, s, answered_now and why.startswith("stale"), now, why)
            self._set(Kind.FROZEN, pair, s, s in frozen, now, frozen.get(s, ""))
            self._set(Kind.OUTLIER, pair, s, why.startswith("outlier"), now, why)
        self._set(
            Kind.QUORUM,
            pair,
            None,
            ref.status != Status.OK,
            now,
            f"status {ref.status.value}, {len(ref.used)} usable source(s)",
        )

        breach = (
            pair in p.stablecoins
            and ref.price is not None
            and abs(ref.price - 1) * 100 > p.depeg_band_pct
        )
        if breach:
            self._depeg_since.setdefault(pair, now)
        else:
            self._depeg_since.pop(pair, None)
        since = self._depeg_since.get(pair)
        confirmed = since is not None and now - since >= p.depeg_confirm_s
        detail = f"reference {ref.price:.4f}" if ref.price is not None else ""
        self._set(Kind.DEPEG, pair, None, confirmed, now, detail)
        return ref

    def _set(
        self, kind: Kind, pair: str, source: str | None, active: bool, now: float, detail: str
    ) -> None:
        key = (kind, pair, source)
        inc = self._open.get(key)
        if active and inc is None:
            inc = Incident(kind, pair, source, now, detail=detail)
            self._open[key] = inc
            self.incidents.append(inc)
            if self.alert:
                self.alert("open", inc)
        elif not active and inc is not None:
            inc.end = now
            del self._open[key]
            if self.alert:
                self.alert("resolved", inc)

    # -- reading -----------------------------------------------------------------------
    def open_incidents(self) -> list[Incident]:
        return list(self._open.values())

    def snapshot(self) -> dict:
        return {
            "prices": {k: v.as_dict() for k, v in self.refs.items()},
            "open_incidents": [i.as_dict() for i in self.open_incidents()],
        }

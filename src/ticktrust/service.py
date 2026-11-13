"""Runs the monitor: live polling of public APIs, or an offline replay of the benchmark market."""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import OrderedDict

from .alerts import Alerter
from .models import Quote, Status
from .monitor import Monitor
from .policy import Policy, load_policy
from .sources import feeds_for, poll

DEFAULT_PAIRS = [
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "USDT-USD",
    "USDC-USD",
    "BTC-INR",
    "ETH-INR",
    "USDT-INR",
]


class QuoteRefused(Exception):
    pass


class Service:
    def __init__(
        self,
        pairs: list[str] | None = None,
        policy: Policy | None = None,
        alerter: Alerter | None = None,
        clock=time.time,
    ):
        self.pairs = pairs or DEFAULT_PAIRS
        self.policy = policy or Policy()
        self.alerter = alerter or Alerter()
        self.monitor = Monitor(self.policy, self.alerter)
        self.feeds = feeds_for(self.pairs)
        self.clock = clock
        self.last_round: float | None = None
        self.errors: dict[str, str] = {}
        self.quotes: OrderedDict[str, dict] = OrderedDict()  # audit trail of issued quotes
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # -- feeding -----------------------------------------------------------------------
    def ingest(self, quotes: list[Quote], now: float | None = None) -> None:
        now = self.clock() if now is None else now
        by_pair: dict[str, list[Quote]] = {p: [] for p in self.pairs}
        for q in quotes:
            by_pair.setdefault(q.pair, []).append(q)
        with self._lock:
            for pair, qs in by_pair.items():
                self.monitor.ingest(pair, qs, now)
            self.last_round = now

    def poll_once(self, get=None) -> None:
        kwargs = {"get": get} if get else {}
        quotes, errors = poll(self.feeds, **kwargs)
        self.errors = {f"{f.source}:{f.pair}": e for f, e in errors}
        self.ingest(quotes)

    def run_live(self, every_s: float = 5.0) -> threading.Thread:
        def loop():
            while not self._stop.is_set():
                started = time.time()
                self.poll_once()
                self._stop.wait(max(0.5, every_s - (time.time() - started)))

        th = threading.Thread(target=loop, daemon=True)
        th.start()
        return th

    def run_replay(self, speed: float = 1.0) -> threading.Thread:
        """Loop the benchmark market, injected faults included, on the wall clock (offline demo)."""
        from .synth import T0, dev

        scenarios = dev()
        self.pairs = [s.pair for s in scenarios]
        self.monitor = Monitor(self.policy, self.alerter)
        n = len(scenarios[0].quotes)

        def loop():
            start = time.time()
            while not self._stop.is_set():
                t = int((time.time() - start) * speed) % n
                now = time.time()
                # keep each quote's age as in the scenario, re-based to now
                rows = [
                    Quote(q.source, q.pair, q.price, now - (T0 + t - q.ts))
                    for s in scenarios
                    for q in s.quotes[t]
                ]
                self.ingest(rows, now)
                self._stop.wait(1 / speed)

        th = threading.Thread(target=loop, daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()

    # -- reading -----------------------------------------------------------------------
    def price(self, pair: str) -> dict | None:
        ref = self.monitor.refs.get(pair)
        return ref.as_dict() if ref else None

    def quote(self, pair: str, fiat_amount: float) -> dict:
        """Price a payment. Refuses rather than quoting on data it cannot trust."""
        ref = self.monitor.refs.get(pair)
        if ref is None:
            raise QuoteRefused(f"no data yet for {pair}")
        if ref.status == Status.HALT or ref.price is None:
            raise QuoteRefused(f"{pair}: no trustworthy source right now ({ref.dropped})")
        if ref.status == Status.DEGRADED and not self.policy.serve_degraded:
            raise QuoteRefused(f"{pair}: only one usable source and policy forbids quoting")
        now = self.clock()
        q = {
            "quote_id": uuid.uuid4().hex[:16],
            "pair": pair,
            "fiat_amount": fiat_amount,
            "price": ref.price,
            "crypto_amount": round(fiat_amount / ref.price, 10),
            "status": ref.status.value,
            "low": ref.low,
            "high": ref.high,
            "sources_used": ref.used,
            "issued_at": now,
            "expires_at": now + self.policy.quote_ttl_s,
        }
        self.quotes[q["quote_id"]] = q
        while len(self.quotes) > 10_000:
            self.quotes.popitem(last=False)
        return q


def from_env() -> Service:
    pairs = [p.strip() for p in os.getenv("TICKTRUST_PAIRS", "").split(",") if p.strip()]
    return Service(pairs or None, load_policy(os.getenv("TICKTRUST_POLICY")))

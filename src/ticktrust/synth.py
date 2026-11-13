"""Synthetic market with known truth and injected feed faults.

Real feed incidents cannot be replayed with ground truth, so the benchmark simulates a
true price per pair, five venues that observe it with latency and noise, and faults with
known start and end times. The *held-out* set uses different magnitudes, timings, noise
and fault combinations, written before TickTrust was run on it and never used for tuning.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .models import Quote

SOURCES = ["coinbase", "kraken", "bitstamp", "gemini", "coingecko"]
T0 = 1_760_000_000.0  # fixed epoch so runs are reproducible


@dataclass
class Fault:
    kind: str  # stale | frozen | flash | bad_tick | outage | drift | depeg
    pair: str
    source: str | None
    start: int  # tick (seconds from scenario start)
    end: int
    size: float = 0.0


@dataclass
class Scenario:
    pair: str
    truth: np.ndarray  # true price per tick
    quotes: list[list[Quote]]  # what each venue answered at each tick
    faults: list[Fault] = field(default_factory=list)


@dataclass
class Profile:
    ticks: int = 14_400  # 4 hours at 1 s
    latency: tuple[float, float] = (0.0, 2.0)
    noise: float = 2e-4  # 2 bp venue-to-venue noise
    update_p: float = 0.8  # chance a venue prints a new price in a given second


PAIRS = {"BTC-USD": (65_000.0, 1.2e-4), "ETH-USD": (3_200.0, 1.6e-4), "USDT-USD": (1.0, 2e-5)}


def _truth(pair: str, n: int, rng, moves: list[tuple[int, int, float]]) -> np.ndarray:
    p0, vol = PAIRS[pair]
    if pair.startswith("USDT"):
        x = np.zeros(n)
        for i in range(1, n):  # mean-reverting around the peg
            x[i] = 0.98 * x[i - 1] + rng.normal(0, vol)
        return p0 * (1 + x)
    steps = rng.normal(0, vol, n)
    for start, dur, size in moves:  # fast market: a real move every venue sees
        steps[start : start + dur] += np.log1p(size) / dur
    return p0 * np.exp(np.cumsum(steps))


def _build(
    pair: str, prof: Profile, rng, faults: list[Fault], moves: list[tuple[int, int, float]]
) -> Scenario:
    n = prof.ticks
    truth = _truth(pair, n, rng, moves)
    for f in faults:  # depeg changes the truth itself: every venue agrees
        if f.kind == "depeg":
            k = np.arange(f.end - f.start)
            ramp = np.minimum(1, np.minimum(k + 1, f.end - f.start - k) / 60)  # in and out
            truth[f.start : f.end] = truth[f.start : f.end] * (1 + f.size * ramp)
    lat = {s: rng.uniform(*prof.latency) for s in SOURCES}
    by_src = {s: [f for f in faults if f.source == s] for s in SOURCES}
    last: dict[str, Quote] = {}
    out: list[list[Quote]] = []
    for t in range(n):
        now = T0 + t
        row = []
        for s in SOURCES:
            seen_t = max(0, t - int(round(lat[s])))
            q = last.get(s)
            if q is None or rng.random() < prof.update_p:
                q = Quote(
                    s, pair, float(truth[seen_t] * (1 + rng.normal(0, prof.noise))), now - lat[s]
                )
            fault = next((f for f in by_src[s] if f.start <= t < f.end), None)
            if fault is None:
                last[s] = q
                row.append(q)
                continue
            k = fault.kind
            if k == "outage":
                continue
            if k == "stale":  # same old response, old timestamp
                q = last.get(s, q)
            elif k == "frozen":  # timestamp keeps moving, price stuck
                q = Quote(s, pair, last.get(s, q).price, now - lat[s])
            elif k == "flash":
                q = Quote(s, pair, q.price * (1 + fault.size), q.ts)
            elif k == "bad_tick":
                q = Quote(s, pair, q.price * fault.size, q.ts)
            elif k == "drift":
                ramp = min(1.0, (t - fault.start + 1) / max(1, (fault.end - fault.start) * 0.6))
                q = Quote(s, pair, q.price * (1 + fault.size * ramp), q.ts)
            if k not in ("stale", "frozen"):
                last[s] = Quote(s, pair, q.price / _undo(fault, t), q.ts)  # keep clean base
            row.append(q)
        out.append(row)
    return Scenario(pair, truth, out, faults)


def _undo(f: Fault, t: int) -> float:
    if f.kind == "flash":
        return 1 + f.size
    if f.kind == "bad_tick":
        return f.size
    if f.kind == "drift":
        return 1 + f.size * min(1.0, (t - f.start + 1) / max(1, (f.end - f.start) * 0.6))
    return 1.0


def _place(rng, n: int, dur: int, busy: list[tuple[int, int]], gap: int = 120) -> int:
    for _ in range(1000):
        s = int(rng.integers(600, n - dur - 600))
        if all(s + dur + gap < a or s > b + gap for a, b in busy):
            busy.append((s, s + dur))
            return s
    raise RuntimeError("could not place fault")


def dev(seed: int = 7) -> list[Scenario]:
    """Tuning set: one faulty venue at a time, typical incident shapes."""
    rng = np.random.default_rng(seed)
    prof = Profile()
    out = []
    for pair in PAIRS:
        n, busy, faults = prof.ticks, [], []
        stable = pair.startswith("USDT")
        plan = [
            ("stale", 420, 0),
            ("stale", 600, 0),
            ("flash", 60, -0.05 if not stable else -0.02),
            ("flash", 90, 0.04 if not stable else 0.015),
            ("bad_tick", 1, 0.1),
            ("bad_tick", 1, 10.0),
            ("outage", 480, 0),
            ("outage", 300, 0),
        ]
        if not stable:
            plan += [("frozen", 900, 0), ("frozen", 600, 0), ("drift", 900, 0.02)]
        else:
            plan += [("depeg", 900, -0.012)]
        for kind, dur, size in plan:
            start = _place(rng, n, dur, busy)
            src = None if kind == "depeg" else str(rng.choice(SOURCES))
            faults.append(Fault(kind, pair, src, start, start + dur, size))
        moves = (
            []
            if stable
            else [
                (_place(rng, n, 90, busy), 90, float(rng.choice([-1, 1])) * rng.uniform(0.02, 0.04))
                for _ in range(3)
            ]
        )
        out.append(_build(pair, prof, rng, faults, moves))
    return out


def held_out(seed: int = 8) -> list[Scenario]:
    """Different noise, latency, sizes and *combined* faults. Never used for tuning."""
    rng = np.random.default_rng(seed)
    prof = Profile(latency=(0.0, 3.0), noise=3e-4, update_p=0.7)
    out = []
    for pair in PAIRS:
        n, busy, faults = prof.ticks, [], []
        stable = pair.startswith("USDT")
        a, b, c = (str(x) for x in rng.choice(SOURCES, 3, replace=False))
        # two venues flash together in the same direction (2 of 5 wrong)
        s = _place(rng, n, 40, busy)
        faults += [
            Fault("flash", pair, a, s, s + 40, -0.03),
            Fault("flash", pair, b, s, s + 40, -0.03),
        ]
        # two venues down, third returns a units error: 1 bad among 3 left
        s = _place(rng, n, 600, busy)
        faults += [
            Fault("outage", pair, a, s, s + 600),
            Fault("outage", pair, b, s, s + 600),
            Fault("bad_tick", pair, c, s + 200, s + 201, 1000.0),
        ]
        s = _place(rng, n, 25, busy)
        faults.append(Fault("flash", pair, c, s, s + 25, 0.08 if not stable else 0.03))
        s = _place(rng, n, 1500, busy)
        faults.append(Fault("stale", pair, b, s, s + 1500))
        moves: list[tuple[int, int, float]] = []
        if not stable:
            # a real crash while one venue is frozen and another is slowly drifting
            s = _place(rng, n, 1800, busy)
            faults += [
                Fault("frozen", pair, a, s, s + 1200),
                Fault("drift", pair, c, s, s + 1800, -0.015),
            ]
            moves.append((s + 300, 300, -0.06))
            s = _place(rng, n, 60, busy)
            moves.append((s, 20, 0.03))
        else:
            s = _place(rng, n, 1800, busy)
            faults += [
                Fault("depeg", pair, None, s, s + 1800, -0.03),
                Fault("stale", pair, a, s + 600, s + 1200),
            ]
        out.append(_build(pair, prof, rng, faults, moves))
    return out

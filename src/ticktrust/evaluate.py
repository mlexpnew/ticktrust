"""Benchmark: would a payment gateway have quoted a wrong price, and was every incident caught?

    python -m ticktrust.evaluate --gate

Strategies, each asked for a price every second for every pair:
  single   one public API (what this repo did originally: CoinGecko only)
  average  mean of the latest price from all five venues (a typical multi-source dashboard)
  ticktrust  this service: median of fresh venues, outliers and frozen feeds removed,
             refuses to quote when too few venues are usable
A quote is *bad* when it is more than 0.5% away from the true price.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .models import Kind, Status
from .monitor import Monitor
from .policy import Policy
from .synth import SOURCES, T0, Scenario, dev, held_out

BAD_PCT = 0.5
NOTIONAL_USD = 1_000  # one $1,000 invoice priced per pair per second
GRACE_S = 60
EXPECTED = {
    "stale": {Kind.STALE},
    "frozen": {Kind.FROZEN},
    "flash": {Kind.OUTLIER},
    "bad_tick": {Kind.OUTLIER},
    "outage": {Kind.OUTAGE},
    "drift": {Kind.OUTLIER},
    "depeg": {Kind.DEPEG},
}


def _new() -> dict:
    return {
        "quotes_asked": 0,
        "served": 0,
        "bad_served": 0,
        "worst_error_pct": 0.0,
        "loss_usd": 0.0,
    }


def _score(acc: dict, price: float | None, truth: float) -> None:
    acc["quotes_asked"] += 1
    if price is None:
        return
    acc["served"] += 1
    err = abs(price / truth - 1) * 100
    acc["worst_error_pct"] = max(acc["worst_error_pct"], err)
    if err > BAD_PCT:
        acc["bad_served"] += 1
        acc["loss_usd"] += NOTIONAL_USD * min(err, 100) / 100  # capped at the invoice value


def run(scenarios: list[Scenario], policy: Policy | None = None) -> dict:
    policy = policy or Policy()
    res = {k: _new() for k in ("single", "average", "ticktrust")}
    res["ticktrust"].update(refused=0, degraded_served=0)
    detection = {
        "faults": 0,
        "detected": 0,
        "by_kind": {},
        "time_to_detect_s": [],
        "false_incidents": 0,
        "missed": [],
    }
    started = time.perf_counter()
    ticks = 0
    for sc in scenarios:
        mon = Monitor(policy)
        last: dict[str, float] = {}
        for t, row in enumerate(sc.quotes):
            now = T0 + t
            for q in row:
                last[q.source] = q.price
            truth = float(sc.truth[t])
            _score(res["single"], last.get("coingecko"), truth)
            _score(res["average"], sum(last.values()) / len(last) if last else None, truth)
            ref = mon.ingest(sc.pair, row, now)
            ok = ref.status == Status.OK or (
                ref.status == Status.DEGRADED and policy.serve_degraded
            )
            _score(res["ticktrust"], ref.price if ok else None, truth)
            if not ok:
                res["ticktrust"]["refused"] += 1
            elif ref.status == Status.DEGRADED:
                res["ticktrust"]["degraded_served"] += 1
            ticks += 1
        _detect(sc, mon, detection)
    for r in res.values():
        r["loss_usd"] = round(r["loss_usd"], 2)
        r["worst_error_pct"] = round(r["worst_error_pct"], 3)
        r["availability"] = round(r["served"] / r["quotes_asked"], 5)
    ttd = sorted(detection.pop("time_to_detect_s"))
    detection["median_time_to_detect_s"] = ttd[len(ttd) // 2] if ttd else None
    detection["recall"] = round(detection["detected"] / detection["faults"], 4)
    res["detection"] = detection
    res["ticks_per_second"] = round(ticks / (time.perf_counter() - started))
    return res


def _detect(sc: Scenario, mon: Monitor, d: dict) -> None:
    incs = mon.incidents
    for f in sc.faults:
        d["faults"] += 1
        k = d["by_kind"].setdefault(f.kind, {"faults": 0, "detected": 0})
        k["faults"] += 1
        lo, hi = T0 + f.start, T0 + f.end + GRACE_S
        hit = [
            i
            for i in incs
            if i.kind in EXPECTED[f.kind]
            and i.pair == f.pair
            and i.source == f.source
            and lo <= i.start <= hi
        ]
        if hit:
            d["detected"] += 1
            k["detected"] += 1
            d["time_to_detect_s"].append(hit[0].start - lo)
        else:
            d["missed"].append(f"{f.pair} {f.kind} {f.source} {f.start}-{f.end}")
    for i in incs:
        end = i.end if i.end is not None else float("inf")
        related = [
            f
            for f in sc.faults
            if f.pair == i.pair
            and (i.source is None or f.source == i.source)
            and i.start <= T0 + f.end + GRACE_S
            and end >= T0 + f.start
        ]
        if not related:
            d["false_incidents"] += 1


def evaluate() -> dict:
    return {"dev": run(dev()), "held_out": run(held_out()), "sources": SOURCES}


def report(r: dict) -> str:
    d, h = r["dev"], r["held_out"]
    rows = []
    for name, label in (
        ("single", "Single API (original repo)"),
        ("average", "Average of 5 venues"),
        ("ticktrust", "**TickTrust**"),
    ):
        a, b = d[name], h[name]
        rows.append(
            f"| {label} | {a['bad_served']:,} | {b['bad_served']:,} | "
            f"${a['loss_usd'] + b['loss_usd']:,.0f} | {a['worst_error_pct']:.2f}% / "
            f"{b['worst_error_pct']:.2f}% | {a['availability'] * 100:.2f}% / "
            f"{b['availability'] * 100:.2f}% |"
        )
    dd, hd = d["detection"], h["detection"]
    kinds = sorted(set(dd["by_kind"]) | set(hd["by_kind"]))
    krows = [
        f"| {k} | {dd['by_kind'].get(k, {}).get('detected', 0)}/"
        f"{dd['by_kind'].get(k, {}).get('faults', 0)} | "
        f"{hd['by_kind'].get(k, {}).get('detected', 0)}/"
        f"{hd['by_kind'].get(k, {}).get('faults', 0)} |"
        for k in kinds
    ]
    return (
        "\n".join(
            [
                f"### Quotes priced: {d['single']['quotes_asked']:,} per set "
                "(3 pairs, 4 hours, 1 per second)",
                "",
                "| Strategy | Bad quotes (dev) | Bad quotes (held-out) | "
                "Loss on bad quotes, both sets ($1,000 each) | Worst error dev / held-out | "
                "Availability dev / held-out |",
                "|---|---|---|---|---|---|",
                *rows,
                "",
                f"Incidents detected: dev {dd['detected']}/{dd['faults']}, held-out "
                f"{hd['detected']}/{hd['faults']}. False incidents: dev {dd['false_incidents']}, "
                f"held-out {hd['false_incidents']}. Median time to detect: dev "
                f"{dd['median_time_to_detect_s']:.0f}s, "
                f"held-out {hd['median_time_to_detect_s']:.0f}s.",
                "",
                "| Fault | Detected (dev) | Detected (held-out) |",
                "|---|---|---|",
                *krows,
            ]
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Benchmark TickTrust against naive price feeds")
    p.add_argument("--out", default="eval_results")
    p.add_argument("--gate", action="store_true", help="fail on any bad dev quote or missed fault")
    a = p.parse_args(argv)
    r = evaluate()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(r, indent=2))
    md = report(r)
    (out / "report.md").write_text(md)
    print(md)
    if a.gate:
        t = r["dev"]["ticktrust"]
        det = r["dev"]["detection"]
        if (
            t["bad_served"]
            or det["recall"] < 1
            or det["false_incidents"]
            or t["availability"] < 0.995
        ):
            print(
                "gate failed: bad quote served, fault missed, false incident or low availability",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

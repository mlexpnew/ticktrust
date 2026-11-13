"""Command line.

ticktrust live                     # poll public APIs once, print reference prices
ticktrust live --watch 5           # keep polling every 5 s, print incidents as they happen
ticktrust bench                    # run the benchmark
"""

from __future__ import annotations

import argparse
import time

from .alerts import Alerter, message
from .policy import load_policy
from .service import DEFAULT_PAIRS, Service


def _table(svc: Service) -> str:
    lines = [f"{'pair':<10}{'price':>16}  {'status':<9}{'sources':<34}dropped"]
    for p in svc.pairs:
        r = svc.price(p)
        if not r:
            continue
        price = f"{r['price']:,.4f}" if r["price"] is not None else "-"
        dropped = ", ".join(f"{k}: {v}" for k, v in r["sources_dropped"].items())
        lines.append(
            f"{p:<10}{price:>16}  {r['status']:<9}{','.join(r['sources_used']):<34}{dropped}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ticktrust")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lv = sub.add_parser("live", help="poll public venue APIs")
    lv.add_argument("--pairs", default=",".join(DEFAULT_PAIRS))
    lv.add_argument("--policy", default=None)
    lv.add_argument("--watch", type=float, default=0, help="seconds between polls; 0 = once")
    sub.add_parser("bench", help="run the benchmark")
    a = ap.parse_args(argv)

    if a.cmd == "bench":
        from .evaluate import main as bench

        return bench([])

    alerter = Alerter(post=lambda _w, _t: None)

    def show(event, inc):
        alerter(event, inc)
        print(time.strftime("%H:%M:%S"), message(event, inc))

    svc = Service([p.strip() for p in a.pairs.split(",")], load_policy(a.policy), alerter)
    svc.monitor.alert = show
    while True:
        svc.poll_once()
        print(_table(svc))
        for k, e in svc.errors.items():
            print(f"  ! {k}: {e}")
        if not a.watch:
            return 0
        time.sleep(a.watch)


if __name__ == "__main__":
    raise SystemExit(main())

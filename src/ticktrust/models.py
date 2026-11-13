"""Data types shared by connectors, the engine, the monitor and the API."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Status(StrEnum):
    OK = "ok"  # enough independent fresh sources agree: safe to quote
    DEGRADED = "degraded"  # only one usable source: quote only if the policy allows it
    HALT = "halt"  # no usable source: refuse to quote


class Kind(StrEnum):
    STALE = "stale"  # source answers, but its price timestamp is old
    FROZEN = "frozen"  # timestamp moves, price does not, while the market does
    OUTAGE = "outage"  # source does not answer at all
    OUTLIER = "outlier"  # fresh price too far from the other sources
    QUORUM = "quorum_lost"  # too few usable sources for an OK reference price
    DEPEG = "depeg"  # stablecoin reference outside its band


@dataclass(frozen=True)
class Quote:
    """One price observation from one source."""

    source: str
    pair: str  # e.g. "BTC-USD"
    price: float
    ts: float  # time the source says the price is from (unix seconds)


@dataclass
class RefPrice:
    pair: str
    price: float | None
    low: float | None  # lowest and highest price among the sources used
    high: float | None
    status: Status
    ts: float
    used: list[str] = field(default_factory=list)
    dropped: dict[str, str] = field(default_factory=dict)  # source -> reason

    @property
    def quotable(self) -> bool:
        return self.price is not None and self.status != Status.HALT

    def as_dict(self) -> dict:
        return {
            "pair": self.pair,
            "price": self.price,
            "low": self.low,
            "high": self.high,
            "status": self.status.value,
            "ts": self.ts,
            "sources_used": self.used,
            "sources_dropped": self.dropped,
        }


@dataclass
class Incident:
    kind: Kind
    pair: str
    source: str | None  # None for pair-level incidents (quorum, depeg)
    start: float
    end: float | None = None
    detail: str = ""

    @property
    def open(self) -> bool:
        return self.end is None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "pair": self.pair,
            "source": self.source,
            "start": self.start,
            "end": self.end,
            "detail": self.detail,
        }

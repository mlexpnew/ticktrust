"""Public price endpoints (no API keys) and their parsers.

Each parser turns one raw JSON response into a Quote with the *source's own* timestamp
where the venue provides one, so stale data can be recognised. Parsers are pure functions
tested against recorded response shapes in tests/fixtures.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from .models import Quote


def _iso(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _num(v) -> float:
    return float(v)


# pair -> venue symbol, per source
SYMBOLS: dict[str, dict[str, str]] = {
    "coinbase": {
        "BTC-USD": "BTC-USD",
        "ETH-USD": "ETH-USD",
        "SOL-USD": "SOL-USD",
        "USDT-USD": "USDT-USD",
    },
    "kraken": {
        "BTC-USD": "XBTUSD",
        "ETH-USD": "ETHUSD",
        "SOL-USD": "SOLUSD",
        "USDT-USD": "USDTUSD",
        "USDC-USD": "USDCUSD",
    },
    "bitstamp": {
        "BTC-USD": "btcusd",
        "ETH-USD": "ethusd",
        "SOL-USD": "solusd",
        "USDT-USD": "usdtusd",
        "USDC-USD": "usdcusd",
    },
    "gemini": {"BTC-USD": "btcusd", "ETH-USD": "ethusd", "SOL-USD": "solusd"},
    "coingecko": {
        "BTC-USD": "bitcoin:usd",
        "ETH-USD": "ethereum:usd",
        "SOL-USD": "solana:usd",
        "USDT-USD": "tether:usd",
        "USDC-USD": "usd-coin:usd",
        "BTC-INR": "bitcoin:inr",
        "ETH-INR": "ethereum:inr",
        "USDT-INR": "tether:inr",
    },
    "binance": {"BTC-USDT": "BTCUSDT", "ETH-USDT": "ETHUSDT", "SOL-USDT": "SOLUSDT"},
    "coindcx": {"BTC-INR": "BTCINR", "ETH-INR": "ETHINR", "USDT-INR": "USDTINR"},
    "wazirx": {"BTC-INR": "btcinr", "ETH-INR": "ethinr", "USDT-INR": "usdtinr"},
}


def url(source: str, symbol: str) -> str:
    if source == "coingecko":
        coin, cur = (symbol.split(":") + ["usd"])[:2]
        return (
            "https://api.coingecko.com/api/v3/simple/price?ids="
            f"{coin}&vs_currencies={cur}&include_last_updated_at=true"
        )
    return {
        "coinbase": f"https://api.exchange.coinbase.com/products/{symbol}/ticker",
        "kraken": f"https://api.kraken.com/0/public/Ticker?pair={symbol}",
        "bitstamp": f"https://www.bitstamp.net/api/v2/ticker/{symbol}/",
        "gemini": f"https://api.gemini.com/v1/pubticker/{symbol}",
        "binance": f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
        "coindcx": "https://api.coindcx.com/exchange/ticker",
        "wazirx": f"https://api.wazirx.com/sapi/v1/ticker/24hr?symbol={symbol}",
    }[source]


def parse(source: str, pair: str, symbol: str, data, now: float) -> Quote:
    """Raw JSON -> Quote. Raises on unexpected shapes so bad responses never become prices."""
    if source == "coinbase":
        price, ts = _num(data["price"]), _iso(data["time"])
    elif source == "kraken":
        if data.get("error"):
            raise ValueError(f"kraken error: {data['error']}")
        (row,) = data["result"].values()
        price, ts = _num(row["c"][0]), now  # Kraken ticker has no timestamp
    elif source == "bitstamp":
        price, ts = _num(data["last"]), float(data["timestamp"])
    elif source == "gemini":
        price, ts = _num(data["last"]), data["volume"]["timestamp"] / 1000
    elif source == "coingecko":
        coin, cur = symbol.split(":")
        price, ts = _num(data[coin][cur]), float(data[coin]["last_updated_at"])
    elif source == "binance":
        price, ts = _num(data["price"]), now  # ticker/price has no timestamp
    elif source == "coindcx":
        row = next(r for r in data if r["market"] == symbol)
        price, ts = _num(row["last_price"]), float(row["timestamp"])
    elif source == "wazirx":
        price, ts = _num(data["lastPrice"]), data["at"] / 1000
    else:
        raise KeyError(source)
    return Quote(source, pair, price, ts)


@dataclass
class Feed:
    source: str
    pair: str

    @property
    def symbol(self) -> str:
        return SYMBOLS[self.source][self.pair]


def feeds_for(pairs: list[str], sources: list[str] | None = None) -> list[Feed]:
    names = sources or list(SYMBOLS)
    return [Feed(s, p) for p in pairs for s in names if p in SYMBOLS.get(s, {})]


def _get(u: str, timeout: float):
    import requests

    r = requests.get(u, timeout=timeout, headers={"User-Agent": "ticktrust/1.0"})
    r.raise_for_status()
    return r.json()


def poll(
    feeds: list[Feed],
    get: Callable = _get,
    timeout: float = 4.0,
    now: Callable[[], float] = time.time,
) -> tuple[list[Quote], list[tuple[Feed, str]]]:
    """Fetch all feeds in parallel. Returns quotes and (feed, error) for failures."""

    def one(f: Feed):
        try:
            return parse(
                f.source, f.pair, f.symbol, get(url(f.source, f.symbol), timeout), now()
            ), None
        except Exception as e:  # network, HTTP, shape: all become an absent answer
            return None, f"{type(e).__name__}: {e}"[:200]

    quotes, errors = [], []
    with ThreadPoolExecutor(max_workers=min(16, len(feeds) or 1)) as ex:
        for f, (q, err) in zip(feeds, ex.map(one, feeds), strict=True):
            if q is not None:
                quotes.append(q)
            else:
                errors.append((f, err))
    return quotes, errors

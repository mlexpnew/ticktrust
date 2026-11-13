import json
from pathlib import Path

import pytest

from ticktrust.service import QuoteRefused, Service
from ticktrust.sources import SYMBOLS, feeds_for, parse, poll, url

RAW = json.loads((Path(__file__).parent / "fixtures" / "responses.json").read_text())
CASES = [
    ("coinbase", "BTC-USD", 67012.34),
    ("kraken", "BTC-USD", 67015.0),
    ("bitstamp", "BTC-USD", 67010.0),
    ("gemini", "BTC-USD", 67012.0),
    ("coingecko", "BTC-USD", 67008.0),
    ("binance", "BTC-USDT", 67020.01),
    ("coindcx", "BTC-INR", 5950000.0),
    ("wazirx", "BTC-INR", 5948000.0),
]


@pytest.mark.parametrize("src,pair,price", CASES)
def test_parsers(src, pair, price):
    qt = parse(src, pair, SYMBOLS[src][pair], RAW[src], now=1763022601.0)
    assert qt.price == price and qt.pair == pair
    assert 1763022590 <= qt.ts <= 1763022601
    assert url(src, SYMBOLS[src][pair]).startswith("https://")


def test_bad_shapes_raise():
    with pytest.raises(ValueError):
        parse("kraken", "BTC-USD", "XBTUSD", {"error": ["EGeneral:Too many requests"]}, 0)
    with pytest.raises(KeyError):
        parse("coinbase", "BTC-USD", "BTC-USD", {"message": "NotFound"}, 0)


def fake_get(u, timeout):
    for src, raw in RAW.items():
        host = url(src, "x").split("/")[2]
        if host in u:
            if src == "gemini":
                raise TimeoutError("read timed out")
            return raw
    raise AssertionError(u)


def test_poll_and_service_quote():
    feeds = feeds_for(["BTC-USD"])
    assert {f.source for f in feeds} == {"coinbase", "kraken", "bitstamp", "gemini", "coingecko"}
    quotes, errors = poll(feeds, get=fake_get, now=lambda: 1763022601.0)
    assert len(quotes) == 4 and errors[0][0].source == "gemini"

    svc = Service(["BTC-USD", "BTC-INR"], clock=lambda: 1763022601.0)
    svc.poll_once(get=fake_get)
    p = svc.price("BTC-USD")
    assert p["status"] == "ok" and 67008 <= p["price"] <= 67015
    qt = svc.quote("BTC-USD", 1000)
    assert abs(qt["crypto_amount"] * qt["price"] - 1000) < 1e-3
    assert svc.quotes[qt["quote_id"]] == qt
    inr = svc.price("BTC-INR")
    assert inr["status"] == "ok" and inr["sources_used"] == ["coindcx", "wazirx"]


def test_service_refuses_when_halted():
    svc = Service(["BTC-USD"], clock=lambda: 1763022601.0)
    with pytest.raises(QuoteRefused):
        svc.quote("BTC-USD", 100)
    svc.poll_once(get=lambda u, t: (_ for _ in ()).throw(ConnectionError("down")))
    with pytest.raises(QuoteRefused):
        svc.quote("BTC-USD", 100)

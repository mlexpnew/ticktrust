from ticktrust.alerts import Alerter, message
from ticktrust.engine import reference
from ticktrust.models import Kind, Quote, Status
from ticktrust.monitor import Monitor
from ticktrust.policy import Policy

P = Policy()


def q(src, price, ts=100.0, pair="BTC-USD"):
    return Quote(src, pair, price, ts)


def test_median_of_fresh_sources():
    r = reference("BTC-USD", [q("a", 100), q("b", 100.5), q("c", 99.5)], 101, P)
    assert r.status == Status.OK and r.price == 100 and r.used == ["a", "b", "c"]


def test_stale_and_outlier_are_dropped():
    r = reference("BTC-USD", [q("a", 100), q("b", 100.1), q("c", 50), q("d", 100, ts=10)], 101, P)
    assert r.price == 100.05
    assert r.dropped["c"].startswith("outlier") and r.dropped["d"].startswith("stale")


def test_two_disagreeing_sources_halt_and_one_source_is_degraded():
    assert reference("BTC-USD", [q("a", 100), q("b", 110)], 101, P).status == Status.HALT
    one = reference("BTC-USD", [q("a", 100), q("b", 100, ts=0)], 101, P)
    assert one.status == Status.DEGRADED and one.price == 100
    assert reference("BTC-USD", [q("a", -1)], 101, P).status == Status.HALT


def test_monitor_outage_stale_outlier_and_alerts():
    sent = []
    al = Alerter(webhook="x", post=lambda w, t: sent.append(t), cooldown_s=0)
    m = Monitor(P, al)
    for t in range(100):
        quotes = [q("a", 100, t), q("b", 100.01, t), q("c", 99.99, t)]
        if 20 <= t < 70:
            quotes = quotes[:2]  # c silent
        if 40 <= t < 45:
            quotes[0] = q("a", 120, t)  # a spikes
        m.ingest("BTC-USD", quotes, float(t))
    kinds = {(i.kind, i.source) for i in m.incidents}
    assert (Kind.OUTAGE, "c") in kinds and (Kind.OUTLIER, "a") in kinds
    assert (Kind.STALE, "c") not in kinds  # silent = outage, not stale
    assert not m.open_incidents()
    assert any("Resolved" in s for s in sent) and any("outage" in s for s in sent)


def test_frozen_feed_detected_only_when_market_moves():
    m = Monitor(P)
    for t in range(200):
        px = 100 * (1 + 0.0001 * t)  # market rises 2%
        m.ingest("BTC-USD", [q("a", px, t), q("b", px, t), q("c", 100.0, t)], float(t))
    froz = [i for i in m.incidents if i.kind == Kind.FROZEN]
    assert froz and froz[0].source == "c"
    assert m.refs["BTC-USD"].dropped["c"].startswith("frozen")


def test_depeg_needs_confirmation():
    m = Monitor(P)
    for t in range(100):
        px = 1.0 if t < 50 else 0.985
        m.ingest("USDT-USD", [q(s, px, t, "USDT-USD") for s in "abc"], float(t))
    dep = [i for i in m.incidents if i.kind == Kind.DEPEG]
    assert len(dep) == 1 and 80 <= dep[0].start <= 81


def test_alert_cooldown_and_message():
    sent = []
    m = Monitor(P, Alerter(webhook="x", post=lambda w, t: sent.append(t), cooldown_s=300))
    for t in range(60):
        bad = t % 2 == 0  # flapping source
        m.ingest("BTC-USD", [q("a", 100, t), q("b", 100, t), q("c", 150 if bad else 100, t)], t)
    opens = [s for s in sent if "outlier" in s and "Resolved" not in s]
    assert len(opens) == 1
    assert "BTC-USD · c" in opens[0]
    assert message("open", m.incidents[0]).startswith("⚠️")

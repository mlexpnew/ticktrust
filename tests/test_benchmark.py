from ticktrust.evaluate import main, run
from ticktrust.synth import dev


def test_dev_benchmark_gate(tmp_path):
    assert main(["--out", str(tmp_path), "--gate"]) == 0
    assert (tmp_path / "report.md").read_text().count("TickTrust") >= 1


def test_ticktrust_beats_naive_on_dev():
    r = run(dev())
    t, single, avg = r["ticktrust"], r["single"], r["average"]
    assert t["bad_served"] == 0 and t["availability"] >= 0.995
    assert single["bad_served"] > 100 and avg["bad_served"] > 100
    assert r["detection"]["recall"] == 1 and r["detection"]["false_incidents"] == 0

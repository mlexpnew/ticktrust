import json
from pathlib import Path

import pytest

from ticktrust.service import Service

RAW = json.loads((Path(__file__).parent / "fixtures" / "responses.json").read_text())


def test_api(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from tests.test_sources import fake_get
    from ticktrust.api import create_app

    monkeypatch.setenv("TICKTRUST_API_KEYS", "k1")
    svc = Service(["BTC-USD", "ETH-USD"], clock=lambda: 1763022601.0)
    svc.poll_once(get=fake_get)
    c = TestClient(create_app(svc, start=False))
    h = {"X-API-Key": "k1"}
    assert c.get("/health").json()["status"] == "ok"
    assert c.get("/v1/prices/BTC-USD").status_code == 401
    assert c.get("/v1/prices/BTC-USD", headers=h).json()["status"] == "ok"
    ok = c.post("/v1/quote", json={"pair": "btc-usd", "fiat_amount": 500}, headers=h)
    assert ok.status_code == 200
    assert c.get(f"/v1/quotes/{ok.json()['quote_id']}", headers=h).status_code == 200
    # no data for this pair: refuse, never guess
    refused = c.post("/v1/quote", json={"pair": "SOL-INR", "fiat_amount": 500}, headers=h)
    assert refused.status_code == 503


def test_dashboard_replay():
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=120).run()
    assert not at.exception
    assert any("TickTrust" in m.label for m in at.metric)

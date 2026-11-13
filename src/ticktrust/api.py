"""HTTP API a payment gateway or app calls before showing a crypto price.

    uvicorn ticktrust.api:app            # live, polls public venue APIs
    TICKTRUST_MODE=replay uvicorn ...    # offline demo on the benchmark market

Set TICKTRUST_API_KEYS (comma separated) to require an X-API-Key header.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .service import QuoteRefused, Service, from_env


class QuoteIn(BaseModel):
    pair: str = Field(examples=["BTC-INR"])
    fiat_amount: float = Field(gt=0, examples=[5000])


def create_app(service: Service | None = None, start: bool = True) -> FastAPI:
    svc = service or from_env()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start:
            mode = os.getenv("TICKTRUST_MODE", "live")
            if mode == "replay":
                svc.run_replay()
            else:
                svc.run_live(float(os.getenv("TICKTRUST_POLL_S", "5")))
        yield
        svc.stop()

    app = FastAPI(
        title="TickTrust",
        version="1.0.0",
        lifespan=lifespan,
        description="Crypto reference prices you can quote on, with feed-integrity alerts.",
    )
    app.state.service = svc

    def auth(x_api_key: str | None = Header(default=None)):
        keys = {k.strip() for k in os.getenv("TICKTRUST_API_KEYS", "").split(",") if k.strip()}
        if keys and x_api_key not in keys:
            raise HTTPException(401, "missing or invalid X-API-Key")

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "pairs": svc.pairs,
            "last_round": svc.last_round,
            "open_incidents": len(svc.monitor.open_incidents()),
        }

    @app.get("/v1/prices", dependencies=[Depends(auth)])
    def prices():
        return {p: svc.price(p) for p in svc.pairs}

    @app.get("/v1/prices/{pair}", dependencies=[Depends(auth)])
    def price(pair: str):
        r = svc.price(pair.upper())
        if r is None:
            raise HTTPException(404, f"no data for {pair}")
        return r

    @app.get("/v1/incidents", dependencies=[Depends(auth)])
    def incidents(open_only: bool = True):
        items = svc.monitor.open_incidents() if open_only else svc.monitor.incidents[-500:]
        return [i.as_dict() for i in items]

    @app.post("/v1/quote", dependencies=[Depends(auth)])
    def quote(body: QuoteIn):
        try:
            return svc.quote(body.pair.upper(), body.fiat_amount)
        except QuoteRefused as e:
            raise HTTPException(503, str(e)) from e

    @app.get("/v1/quotes/{quote_id}", dependencies=[Depends(auth)])
    def get_quote(quote_id: str):
        q = svc.quotes.get(quote_id)
        if q is None:
            raise HTTPException(404, "unknown quote")
        return q

    return app


app = create_app()

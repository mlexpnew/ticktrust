"""Risk policy: the thresholds a payments or risk team signs off on. Loadable from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Policy(BaseModel):
    max_age_s: float = Field(15, description="older price timestamps are not used")
    outage_after_s: float = Field(30, description="no answer for this long = outage")
    max_dev_pct: float = Field(1.0, description="drop a source this far from the median")
    min_sources: int = Field(2, description="fresh agreeing sources needed for status OK")
    serve_degraded: bool = Field(True, description="quote on a single source (status DEGRADED)")
    frozen_after_s: float = Field(60, description="price unchanged this long ...")
    frozen_market_move_pct: float = Field(
        0.1, description="... while the reference moved this much"
    )
    stablecoins: list[str] = Field(default_factory=lambda: ["USDT-USD", "USDC-USD"])
    depeg_band_pct: float = Field(0.5, description="stablecoin alert outside 1 +/- band")
    depeg_confirm_s: float = Field(30, description="band must be breached this long")
    quote_ttl_s: int = Field(60, description="how long a payment quote is honoured")


def load_policy(path: str | Path | None) -> Policy:
    if not path:
        return Policy()
    data = yaml.safe_load(Path(path).read_text()) or {}
    return Policy(**data.get("policy", data))

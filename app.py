"""TickTrust dashboard.  streamlit run app.py

Replay: the benchmark market with injected feed faults (works offline).
Live: polls public venue APIs and shows the reference price, status and incidents.
"""

from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from ticktrust.evaluate import BAD_PCT
from ticktrust.models import Status
from ticktrust.monitor import Monitor
from ticktrust.policy import Policy
from ticktrust.service import DEFAULT_PAIRS, Service
from ticktrust.synth import SOURCES, T0, dev, held_out

st.set_page_config(page_title="TickTrust", page_icon="📈", layout="wide")
st.title("TickTrust")
st.caption(
    "Crypto reference prices you can quote on. Stale, frozen, broken and depegged feeds "
    "are caught before a customer sees a wrong price."
)

mode = st.sidebar.radio("Mode", ["Replay (offline demo)", "Live (public APIs)"])


@st.cache_data(show_spinner="Replaying 4 hours of market data...")
def replay(which: str) -> tuple[dict, dict]:
    scenarios = dev() if which == "dev" else held_out()
    frames, incidents = {}, {}
    for sc in scenarios:
        mon = Monitor(Policy())
        last: dict[str, float] = {}
        rows = []
        for t, quotes in enumerate(sc.quotes):
            for q in quotes:
                last[q.source] = q.price
            ref = mon.ingest(sc.pair, quotes, T0 + t)
            rows.append(
                {
                    "t": t,
                    "truth": sc.truth[t],
                    "single_api": last.get("coingecko"),
                    "average": sum(last.values()) / len(last),
                    "ticktrust": ref.price,
                    "status": ref.status.value,
                    **{s: last.get(s) for s in SOURCES},
                }
            )
        frames[sc.pair] = pd.DataFrame(rows)
        incidents[sc.pair] = [i.as_dict() for i in mon.incidents]
    return frames, incidents


if mode.startswith("Replay"):
    which = st.sidebar.selectbox(
        "Market",
        ["dev", "held_out"],
        format_func=lambda x: {
            "dev": "Benchmark (tuning set)",
            "held_out": "Held-out (never tuned on)",
        }[x],
    )
    frames, incidents = replay(which)
    pair = st.sidebar.selectbox("Pair", list(frames))
    df, inc = frames[pair], incidents[pair]

    def bad(col):
        return int(((df[col] / df["truth"] - 1).abs() * 100 > BAD_PCT).sum())

    c = st.columns(4)
    c[0].metric("Bad quotes: single API", f"{bad('single_api'):,}")
    c[1].metric("Bad quotes: average of 5", f"{bad('average'):,}")
    c[2].metric("Bad quotes: TickTrust", f"{bad('ticktrust'):,}")
    c[3].metric("Incidents raised", len(inc))
    st.caption(
        f"A bad quote is more than {BAD_PCT}% from the true price, priced every second for 4 hours."
    )

    view = df.iloc[::10].set_index("t")[["truth", "single_api", "average", "ticktrust"]]
    st.subheader("Price over 4 hours")
    st.line_chart(view)

    st.subheader("Incidents")
    if inc:
        idf = pd.DataFrame(inc)
        idf["start"] = (idf["start"] - T0).astype(int)
        idf["end"] = (idf["end"] - T0).fillna(-1).astype(int)
        idf["minutes"] = ((idf["end"] - idf["start"]) / 60).round(1)
        st.dataframe(
            idf[["kind", "source", "start", "end", "minutes", "detail"]],
            use_container_width=True,
            hide_index=True,
        )
        pick = st.selectbox(
            "Zoom into an incident",
            range(len(idf)),
            format_func=lambda i: f"{idf.kind[i]} · {idf.source[i] or pair} at {idf.start[i]}s",
        )
        s = int(idf.start[pick])
        win = df[(df.t >= s - 120) & (df.t <= s + 240)].set_index("t")
        st.line_chart(win[[*SOURCES, "ticktrust"]])
        st.caption("Each venue's last price and the TickTrust reference around the incident.")
else:
    pairs = st.sidebar.multiselect("Pairs", DEFAULT_PAIRS, DEFAULT_PAIRS[:5])
    every = st.sidebar.slider("Refresh every (s)", 5, 60, 10)
    auto = st.sidebar.toggle("Auto refresh", value=False)
    if "svc" not in st.session_state or st.session_state.get("pairs") != pairs:
        st.session_state.svc = Service(pairs)
        st.session_state.pairs = pairs
    svc: Service = st.session_state.svc
    if st.button("Poll now") or auto or svc.last_round is None:
        svc.poll_once()
    rows = []
    for p in pairs:
        r = svc.price(p)
        if r:
            rows.append(
                {
                    "pair": p,
                    "price": r["price"],
                    "status": r["status"],
                    "low": r["low"],
                    "high": r["high"],
                    "sources": ", ".join(r["sources_used"]),
                    "dropped": "; ".join(f"{k}: {v}" for k, v in r["sources_dropped"].items()),
                }
            )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    bad_pairs = [r["pair"] for r in rows if r["status"] != Status.OK.value]
    if bad_pairs:
        st.warning("Not safe to quote at full confidence: " + ", ".join(bad_pairs))
    opened = svc.monitor.open_incidents()
    st.subheader(f"Open incidents ({len(opened)})")
    if opened:
        st.dataframe(pd.DataFrame([i.as_dict() for i in opened]), hide_index=True)
    if svc.errors:
        with st.expander(f"{len(svc.errors)} source errors"):
            st.json(svc.errors)
    if auto:
        time.sleep(every)
        st.rerun()

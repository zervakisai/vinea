"""What the nights cost, and which number is only an estimate.

The panel exists to answer three questions an operator actually asks: what did
last night cost, which tenant costs the most, and is the cache doing anything.

It also answers a fourth the router left hanging. `BORDERLINE_FRACTION_OF_RAW`
was described there as "a cost/quality dial disguised as a threshold", and until
now there was no way to see the cost half of the dial. There is one here -- but
it is a *counterfactual*, not a measurement, and the panel says so on screen
rather than presenting an estimate in the same typeface as a fact.

Two rules this panel is built around:

  **NULL is not zero.** A night that called no model has `cost_usd = NULL`.
  Averaging with those treated as 0.0 quietly divides by the wrong denominator
  and reports a cost per advisory that no invoice will ever agree with. Every
  aggregate below is computed over the rows that *have* the number, and the count
  of rows that do not is shown beside it.

  **The API is the only source.** ADR-005: this panel gets rows from
  `/ops/advisories` and aggregates in the browser process, exactly as the quality
  monitor does. No database handle lives in the UI.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from vinea.ui.client import ApiClient


def render(client: ApiClient) -> None:
    st.header("Cost")
    st.caption("Spend per advisory, from the gateway's own accounting — not a price table.")

    days = st.slider("Window (days)", min_value=7, max_value=180, value=30, step=7)
    end = date.today()
    rows = client.all_advisories(start=end - timedelta(days=days), end=end, limit=5000)
    if not rows:
        st.info("No advisories in this window.")
        return

    df = pd.DataFrame(rows)
    df["run_date"] = pd.to_datetime(df["run_date"])
    for column in ("cost_usd", "input_tokens", "output_tokens", "cache_hit"):
        if column not in df:
            # An API older than this panel. Better an empty column than a
            # KeyError: the additive contract is only worth anything if the
            # reader tolerates its absence.
            df[column] = None

    priced = df[df["cost_usd"].notna()]
    unpriced = len(df) - len(priced)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Advisories", len(df))
    c2.metric("Total cost", f"${priced['cost_usd'].sum():.2f}" if len(priced) else "—")
    c3.metric(
        "Mean per priced advisory",
        f"${priced['cost_usd'].mean():.4f}" if len(priced) else "—",
    )
    # Deliberately prominent rather than a footnote. This number is the honesty
    # of every other number on the row: if most advisories are unpriced, the
    # totals describe a minority of the nights.
    c4.metric("No cost recorded", unpriced, help="Model skipped, no key, or no gateway to report cost.")

    if not len(priced):
        st.info(
            "No advisory in this window carries a cost. Cost is reported by the gateway; "
            "with `VINEA_GATEWAY_URL` unset the columns stay NULL by design."
        )
        return

    st.subheader("Cost per night")
    st.bar_chart(priced.groupby("run_date")["cost_usd"].sum())

    st.subheader("Cost by tenant")
    by_tenant = (
        priced.groupby("tenant")
        .agg(advisories=("cost_usd", "size"), total_usd=("cost_usd", "sum"), mean_usd=("cost_usd", "mean"))
        .sort_values("total_usd", ascending=False)
    )
    st.dataframe(by_tenant, use_container_width=True)

    st.subheader("Cache")
    cached = df["cache_hit"].notna() & (df["cache_hit"] == True)  # noqa: E712 -- pandas mask, not a bool test
    reported = df["cache_hit"].notna().sum()
    if reported:
        st.metric("Fully cached advisories", f"{cached.sum()}/{reported}")
        st.caption(
            "'Fully cached' means every model call in the advisory was served from cache. "
            "Two thirds cached still bought a completion, so this is deliberately strict."
        )
    else:
        st.caption("No advisory reported a cache status — the gateway's cache is off, or absent.")

    st.subheader("What the router avoided")
    model_free = df[df["cost_usd"].isna() & ~df["degraded"]]
    st.caption(
        "`BORDERLINE_FRACTION_OF_RAW` is a cost/quality dial disguised as a "
        "threshold. These are the nights it decided were clear-cut enough to answer without "
        "a model — complete advisories, not degraded ones."
    )
    estimate = len(model_free) * priced["cost_usd"].mean()
    st.metric("Model-free advisories", len(model_free))
    st.metric("Estimated avoided spend", f"${estimate:.2f}")
    st.warning(
        "**Estimate, not a measurement.** It assumes each skipped night would have cost the "
        "mean of the nights that did call a model. Those are exactly the *harder* nights, so "
        "if anything this over-states the saving. Shown because a dial nobody can see gets "
        "turned by guesswork; labelled because a counterfactual in a metric card gets quoted "
        "as a fact by the second meeting."
    )

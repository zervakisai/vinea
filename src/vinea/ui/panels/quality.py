"""The quality monitor: confidence distribution, degraded rate, trace links.

Cross-tenant health at a glance: how confident are the advisories, what fraction ran
degraded (no model), and a table linking each to its trace. All aggregated
client-side from the ops advisory feed -- the API serves rows, the UI does the
arithmetic.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from vinea.ui.client import ApiClient, langfuse_trace_url


def render(client: ApiClient) -> None:
    st.header("Quality monitor")
    st.caption("Confidence, degraded rate, and trace links across all tenants.")

    rows = client.all_advisories(limit=1000)
    if not rows:
        st.info("No advisories yet. Enqueue some and run a worker.")
        return

    df = pd.DataFrame(rows)
    total = len(df)
    degraded = int(df["degraded"].sum())

    c1, c2, c3 = st.columns(3)
    c1.metric("Advisories", total)
    # Degraded rate: the fraction produced with no model. High is not necessarily
    # bad (clear-cut days route to deterministic), but a spike means keys are
    # missing or the router is over-skipping.
    c2.metric("Degraded rate", f"{degraded / total:.0%}")
    with_conf = df["overall_confidence"].dropna()
    c3.metric("Median confidence", f"{with_conf.median():.2f}" if len(with_conf) else "—")

    st.subheader("Confidence distribution")
    if len(with_conf):
        # Bucket into 0.1-wide bins so the distribution reads at a glance. Label the
        # bins as strings ("0.0–0.1", ...): pandas Interval objects can't be
        # serialised by altair, which crashes the chart -- caught by AppTest,
        # invisible to a "did the server boot" check.
        edges = [i / 10 for i in range(11)]
        labels = [f"{edges[i]:.1f}–{edges[i + 1]:.1f}" for i in range(len(edges) - 1)]
        binned = pd.cut(with_conf, bins=edges, labels=labels, include_lowest=True)
        counts = binned.value_counts().reindex(labels, fill_value=0)
        st.bar_chart(counts)
    else:
        st.write("No confidence values recorded yet.")

    st.subheader("Recent advisories")
    display = df[["tenant", "run_date", "degraded", "overall_confidence", "trace_id"]].copy()
    display["trace"] = display["trace_id"].apply(lambda t: langfuse_trace_url(t) if t else None)
    st.dataframe(
        display.drop(columns=["trace_id"]),
        column_config={"trace": st.column_config.LinkColumn("trace", display_text="🔎 view")},
        hide_index=True,
    )

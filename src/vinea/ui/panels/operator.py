"""S6.3 -- the operator overview: queue depth over time, and nightly counts.

The panel that makes "autoscale on queue depth" (DESIGN.md B1) a thing you can look
at: a chart of queued/running/failed over time, drawn from the samples S3.4 stored.
All from the ops endpoints; the panel never touches the queue table directly.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from vinea.ui.client import ApiClient


def render(client: ApiClient) -> None:
    st.header("Operator overview")
    st.caption("Queue depth over time — the signal you autoscale on (not CPU).")

    depth = client.queue_depth()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Queued", depth["queued"])
    c2.metric("Running", depth["running"])
    c3.metric("Done", depth["done"])
    c4.metric("Failed", depth["failed"], delta=None)

    history = client.queue_history(limit=1000)
    if not history:
        st.info("No queue-depth samples yet. Run a worker (it samples as it drains) to populate the chart.")
        return

    df = pd.DataFrame(history)
    df["sampled_at"] = pd.to_datetime(df["sampled_at"])
    df = df.sort_values("sampled_at").set_index("sampled_at")

    st.subheader("Queue depth over time")
    # queued/running/failed on the same axis: the shape an autoscaler reads.
    st.line_chart(df[["queued", "running", "failed"]])

    st.subheader("Throughput")
    st.area_chart(df[["done"]])

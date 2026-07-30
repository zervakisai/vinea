"""The operator overview: queue depth over time, nightly counts, and the SLOs.

The panel that makes "autoscale on queue depth" a thing you can look at: a chart of
queued/running/failed over time, drawn from the samples the worker stores.
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

    st.divider()
    render_slo(client)


def render_slo(client: ApiClient) -> None:
    """The three objectives, their budgets, and what a spent budget means.

    Read through the API like every other panel (ADR-005). The policy string comes
    from the same call that reports the breach, so "what do we do now" is not
    something anyone has to remember from a meeting.
    """
    st.subheader("Service level objectives")

    try:
        objectives = client.slo()
    except Exception as exc:  # noqa: BLE001 -- a panel must not take the app down
        st.warning(f"Could not read /ops/slo: {type(exc).__name__}")
        return

    for objective in objectives:
        value, met = objective.get("value"), objective.get("met")
        unit = objective.get("unit", "")
        label = objective["key"].replace("_", " ")

        if value is None:
            # Declared, agreed, not collected -- and shown as such. "No advisories
            # were late" and "we could not tell" must not look the same.
            st.metric(label, "not measured", help=objective.get("description"))
            st.caption(f"target {objective['target']:g} {unit} · no samples in window")
            continue

        shown = f"{value:.3g}" if unit == "milliseconds" else f"{value * 100:.1f}%"
        target = (
            f"{objective['target']:g} ms"
            if unit == "milliseconds"
            else f"{objective['target'] * 100:.0f}%"
        )
        st.metric(
            label,
            shown,
            delta="within target" if met else "BREACH",
            delta_color="normal" if met else "inverse",
            help=objective.get("description"),
        )
        caption = f"target {target} · n={objective['sample_size']}"
        if objective.get("budget_allowed") is not None:
            remaining = objective["budget_remaining"]
            caption += (
                f" · budget {objective['budget_observed']}/"
                f"{objective['budget_allowed']:.1f} used"
                f"{', EXHAUSTED' if objective['budget_exhausted'] else f', {remaining:.1f} left'}"
            )
        st.caption(caption)
        if objective.get("budget_exhausted") and objective.get("policy"):
            st.error(objective["policy"])

"""The grower advisory card, and its trace deep-link.

What a grower actually reads: today's irrigation call, the safe spray windows,
confidence per leg, the caveats, and a degraded badge when a model wasn't involved.
Everything comes from one `get_advisory` call; the panel renders, it does not
compute.
"""

from __future__ import annotations

from datetime import date

import streamlit as st

from vinea.ui.client import ApiClient, langfuse_trace_url


def render(client: ApiClient) -> None:
    st.header("Grower view")
    st.caption("Pick a block and a date. Everything here is read from the API.")

    col1, col2 = st.columns(2)
    tenant = col1.text_input("Tenant", value="acme")
    run_date = col2.date_input("Run date", value=date(2025, 2, 8))

    if st.button("Enqueue this advisory"):
        result = client.enqueue(tenant, run_date)
        st.success(
            f"Task #{result['task_id']} {result['status']}"
            + (" (already queued)" if result["already_queued"] else "")
        )

    envelope = client.get_advisory(tenant, run_date)
    if envelope is None:
        st.info("No advisory yet for this block and date. Enqueue it, then let a worker run.")
        return

    _advisory_card(envelope)


def _advisory_card(envelope: dict) -> None:
    advisory = envelope["advisory"]
    irrigation = advisory["irrigation"]
    spray = advisory["spray"]

    # The degraded badge: says out loud when no model was involved.
    if envelope["degraded"]:
        st.warning("⚠️ Degraded advisory — produced deterministically, no model was called.")

    st.subheader(f"Advisory for {advisory['date']}")

    left, right = st.columns(2)
    with left:
        st.markdown("**Irrigation**")
        if irrigation["should_irrigate_tomorrow"]:
            st.metric("Irrigate", f"{irrigation['recommended_depth_mm']:.1f} mm")
        else:
            st.metric("Irrigate", "No")
        st.caption(f"depletion {irrigation['current_depletion_mm']:.1f} mm")
        st.progress(min(1.0, irrigation["confidence"]), text=f"confidence {irrigation['confidence']:.2f}")
        st.write(irrigation["rationale"])

    with right:
        st.markdown("**Spray**")
        windows = spray["recommended_windows"]
        if windows:
            for w in windows:
                st.write(f"🕑 {w['start']} → {w['end']}  ({w['reason']})")
        else:
            st.write("No safe window today.")
            if spray.get("limiting_factors"):
                st.caption("limiting: " + "; ".join(spray["limiting_factors"]))
        st.progress(min(1.0, spray["confidence"]), text=f"confidence {spray['confidence']:.2f}")
        st.write(spray["rationale"])

    st.markdown("**Plan**")
    overall = advisory["overall_confidence"]
    st.progress(min(1.0, overall), text=f"overall confidence {overall:.2f}")
    st.write(advisory["summary"])
    for fact in advisory["conflicts_resolved"]:
        st.caption(f"• {fact}")

    # The sources, labelled precisely.
    #
    # "Shown to the model", not "used by the model". `advisory_citations` records
    # what retrieval supplied; asking the model which sources it used would be a
    # self-report, and self-report is not evidence. The
    # weaker wording is the honest one, and putting the stronger wording on a
    # grower's screen would be the phase's own failure mode.
    citations = envelope.get("citations") or []
    if citations:
        st.subheader("Sources shown to the model")
        st.caption(
            "Passages from FAO Irrigation and Drainage Paper 56 (CC BY 4.0) that were "
            "supplied as background. They explain the reasoning; every number above is "
            "computed from this block's own weather and configuration."
        )
        by_leg: dict[str, list[str]] = {}
        for citation in sorted(citations, key=lambda c: (c["leg"], c["rank"])):
            by_leg.setdefault(citation["leg"], []).append(citation["locator"])
        for leg, locators in by_leg.items():
            st.caption(f"**{leg}** — " + " · ".join(locators))

    trace_id = envelope.get("trace_id")
    if trace_id:
        st.link_button("🔎 View trace in Langfuse", langfuse_trace_url(trace_id))
    st.caption(f"model: {envelope.get('model_id') or '—'} · prompt: {envelope.get('prompt_version') or '—'}")

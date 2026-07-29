"""The Streamlit entrypoint. Run: uv run streamlit run src/vinea/ui/app.py

A sidebar picks one of the panels; each is a pure function of API data. The app
builds one `ApiClient` from the environment and hands it to whichever panel is
selected -- so every byte on screen came through the API, and this file has no
other data source.
"""

from __future__ import annotations

import streamlit as st

from vinea.ui.client import ApiClient
from vinea.ui.panels import cost, grower, operator, quality


def main() -> None:
    st.set_page_config(page_title="Vinea Advisory", page_icon="🍇", layout="wide")
    st.title("🍇 Vinea Advisory")

    client = ApiClient.from_env()

    with st.sidebar:
        st.markdown("### Panels")
        panel = st.radio(
            "View",
            ["Grower view", "Operator overview", "Quality monitor", "Cost"],
            label_visibility="collapsed",
        )
        st.divider()
        st.caption(f"API: `{client.base_url}`")
        try:
            health = client.health()
            ok = health.get("database") == "ok"
            chip = "🟢" if ok else "🟠"
            st.caption(f"{chip} API {health.get('status', '?')}, DB {health.get('database', '?')}")
        except Exception:  # noqa: BLE001 -- the health chip must never crash the app
            st.caption("🔴 API unreachable")

    if panel == "Grower view":
        grower.render(client)
    elif panel == "Operator overview":
        operator.render(client)
    elif panel == "Cost":
        cost.render(client)
    else:
        quality.render(client)


if __name__ == "__main__":
    main()

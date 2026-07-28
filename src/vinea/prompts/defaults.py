"""The bundled default templates. The fail-open floor.

These are the exact framings that `agents.py`'s `render_*_context` functions used
to build as f-strings, turned into `{{variable}}` templates. They ship *in the
deploy artifact*, so when the registry is unreachable and the cache is cold, the
system still has a coherent prompt to run -- yesterday's phrasing, never a crash
(DESIGN.md B3's fail-open floor).

Two invariants hold these together:

  1. **No numbers.** A template is framing plus `{{placeholders}}`. The day's
     config and computed depletion are substituted locally at render time
     (registry.py) from the agent's deps. Nothing here reaches the registry that
     could leak a grower's numbers, and an outage can only cost wording.

  2. **They match production.** The CI drift check (S7.3) diffs these against
     whatever the registry serves at `@production`. If an agronomist ships a new
     production version and forgets to update the bundled default, the floor
     silently rots -- the drift check fails the build before that ships.

The variable names are the contract between a template and its render call. Rename
`{{crop}}` to `{{crop_name}}` here without updating the caller and the
typed-variable check in registry.py fails loudly at render, not three garbled words
into a prompt in front of a grower (B3-4).
"""

from __future__ import annotations

# Prompt names, used as the registry key and the bundled-default key. The
# `@label` (production/staging) is chosen by the caller, not baked in here.
IRRIGATION = "agronomy_irrigation"
SPRAY = "agronomy_spray"
COORDINATOR = "agronomy_coordinator"


BUNDLED_DEFAULTS: dict[str, str] = {
    IRRIGATION: (
        "Crop: {{crop}} ({{irrigation_method}}-irrigated). As of {{run_date}}, "
        "advising for {{target_date}}. "
        "Injected config: Kc={{kc}}, RAW/MAD trigger={{raw_mm}} mm, TAW={{taw_mm}} mm, "
        "refill cap (field capacity)={{taw_mm}} mm. "
        "Computed (DO NOT recompute): current_depletion_mm={{current_depletion_mm}}. "
        "Data quality: {{dq_note}}."
    ),
    SPRAY: (
        "Crop: {{crop}}, spray sensitivity: {{spray_sensitivity}}. As of {{run_date}}, "
        "advising for {{target_date}}. "
        "Injected thresholds: Delta T ideal {{deltat_ideal_low}}-{{deltat_ideal_high}} °C, "
        "marginal {{deltat_ideal_high}}-{{deltat_marginal_upper}} °C, "
        ">{{deltat_marginal_upper}} unsuitable, <{{deltat_inversion_below}} inversion; wind ideal "
        "{{wind_ideal_low}}-{{wind_ideal_high}} m/s; Spray Index {{spray_index_direction}}, "
        "cutoff {{spray_index_cutoff}}. "
        "Data quality: {{dq_note}}."
    ),
    COORDINATOR: (
        "Crop: {{crop}}, {{irrigation_method}}-irrigated, deficit-irrigation strategy. As of "
        "{{run_date}}, advising for {{target_date}}. Reconcile the two typed sub-advices into one "
        "coherent, sequenced day; explain interactions in plain grower language. "
        "Data quality: {{dq_note}}."
    ),
}

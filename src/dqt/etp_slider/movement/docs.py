"""Movement-category copy and classification flowchart for drift report explorer."""

from __future__ import annotations

from typing import Any

from dqt.etp_slider.movement.orders import (
    CLOCK_DELTA_MIN,
    LEGACY_LEADTIME_CATEGORY,
)

# Node ids must match CLASSIFICATION_FLOWCHART (used for Mermaid highlight).
FLOWCHART_NODE_BY_CATEGORY: dict[str, str] = {
    "ShipmentChange": "S",
    "DifficultyOverride": "DO",
    "LeadtimeChange": "LC",
    LEGACY_LEADTIME_CATEGORY: "LC",
    "RandomChange": "R",
    "Unclassified": "U",
}

CLASSIFICATION_FLOWCHART = f"""flowchart TD
  A["Problem load<br/>large ETP50 avail→48h shift"] --> B{{Davis enrichment<br/>row present?}}
  B -->|No| U[Unclassified]
  B -->|Yes| C{{charge_inc_ind?<br/>Δ charges > $50}}
  C -->|Yes| S[ShipmentChange]
  C -->|No| C2{{equip_change_ind?<br/>EquipmentType avail≠48hr}}
  C2 -->|Yes| S
  C2 -->|No| D{{hard_ft_inc_ind?<br/>Δ hard_ft ≥ 1}}
  D -->|Yes| DO[DifficultyOverride]
  D -->|No| E{{clocks_moved_ind?<br/>book_2_pkup or avail_2_book Δ ≥ {CLOCK_DELTA_MIN}h}}
  E -->|Yes| LC[LeadtimeChange]
  E -->|No| F{{index_change_ind?<br/>DAT / fuel / lag7 moved}}
  F -->|Yes| R[RandomChange]
  F -->|No| R2["RandomChange<br/>catch-all residual"]"""

CLASSIFICATION_PRIORITY_NOTE = (
    "One label per load — first matching gate wins. If charges increased >$50, the load "
    "is ShipmentChange even when clocks or DAT/fuel also moved; those lower gates are "
    "not shown as the primary reason."
)

CATEGORY_DOCS: dict[str, dict[str, Any]] = {
    "ShipmentChange": {
        "label": "Shipment Change",
        "tagline": "Customer or shipment economics changed during the wait window.",
        "summary": (
            "The model repriced after a material change in customer total charges "
            "(or other shipment attributes). On loads like this, the charge move is "
            "the assigned explanation — not lead-time clocks or market indices."
        ),
        "criteria": [
            "Gate: total_charges Δ > $50 (charge_inc_ind), or EquipmentType changed (equip_change_ind).",
            "EquipmentType = model log codes (V/R/PO/…); not core.loads.load_type (DRY/REEFER).",
            "Often large $ moves when charges jump materially.",
        ],
        "features": ["total_charges", "load_type", "location", "equipment", "appointments"],
    },
    "DifficultyOverride": {
        "label": "Difficulty Override",
        "tagline": "Difficulty restrictions increased — harder freight to cover.",
        "summary": (
            "Rare but often large per-load moves when hard_ft_cnt jumps. The model "
            "widens the band when restrictions like flamed, TWIC, HRHV, or MDE appear."
        ),
        "criteria": [
            "Gate: hard_ft Δ ≥ 1 (hard_ft_inc_ind).",
            "Only reached when charge_inc_ind did not fire.",
        ],
        "features": ["hard_ft_cnt", "is_flamed", "is_hrhv", "TWIC-style restrictions"],
    },
    "LeadtimeChange": {
        "label": "Lead Time Change",
        "tagline": "Lead-time clocks moved — expected repricing as pickup approaches.",
        "summary": (
            "Dominant bucket on 7–14 day lead loads. book_2_pkup and avail_2_book "
            "advance every hour; the model reprices with these clocks. This is the "
            "core lead-time drift the analysis targets."
        ),
        "criteria": [
            f"Gate: |book_2_pkup Δ| or |avail_2_book Δ| ≥ {CLOCK_DELTA_MIN} hours.",
            "Only reached when shipment, equip, and difficulty gates did not fire.",
            "clock_only_ind = clocks moved with no charge/equip/difficulty flags.",
            "Path shapes (sub-breakdown): late_cliff, mid_plateau, steady, early_burst from etp50_idx checkpoint deltas.",
        ],
        "features": ["book_2_pkup", "avail_2_book", "hours_since_available"],
    },
    "RandomChange": {
        "label": "Random / Market Change",
        "tagline": "Market indices moved, or no higher-priority signal was detected.",
        "summary": (
            "Two paths: index-detected (DAT, fuel, lag7 CPM endpoint deltas) or "
            "catch-all residual when shipment, difficulty, and clocks did not fire. "
            "Pooled net effect is expected ~$0; individual loads can still spike."
        ),
        "criteria": [
            "Index gate: DAT / fuel / lag7 CPM Δ ≥ 10% of baseline (avail→48hr).",
            "Catch-all gate: no charge, difficulty, or clock signal fired.",
            "Only reached when all higher gates did not fire.",
        ],
        "features": ["dat_rate", "fuel_cost", "lag7_cpm"],
    },
    "Unclassified": {
        "label": "Unclassified",
        "tagline": "Load missing from the Davis enrichment join.",
        "summary": (
            "No movement flags could be computed — typically the load falls outside "
            "the Davis cache window or the cache was not built for this avail range."
        ),
        "criteria": [
            "total_charges_delta is null (no Davis row).",
            "Rebuild analysis-davis cache for the report avail window.",
        ],
        "features": [],
    },
}

CATEGORY_PRIORITY: tuple[str, ...] = (
    "ShipmentChange",
    "DifficultyOverride",
    "LeadtimeChange",
    "RandomChange",
    "Unclassified",
)


def build_movement_category_docs() -> dict[str, Any]:
    """Payload for drift report explorer category panel."""
    categories = dict(CATEGORY_DOCS)
    categories[LEGACY_LEADTIME_CATEGORY] = categories["LeadtimeChange"]
    return {
        "flowchart": CLASSIFICATION_FLOWCHART,
        "priority": list(CATEGORY_PRIORITY),
        "priority_note": CLASSIFICATION_PRIORITY_NOTE,
        "categories": categories,
        "flowchart_nodes": FLOWCHART_NODE_BY_CATEGORY,
    }

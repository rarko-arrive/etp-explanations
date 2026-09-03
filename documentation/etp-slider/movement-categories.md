# ETP Slider Movement Categories

Public-facing taxonomy for drift reports, Davis analysis, and leadership readouts.

## Problem load definition (drift report tail)

Configurable via `TailThresholdConfig` / `--tail-mode` on `build_etp_drift_report.py`.

| Mode | Rule | When to use |
|------|------|-------------|
| **`and`** (default) | ≥10% pct shift (default build); add `--tail-amt 75` for ≥10% **and** ≥$75 | Stricter with $ floor |
| **`or`** | ≥10% **or** ≥$75 | Inclusive; ~2× cohort vs pct-only |
| **`scaled`** | shift ≥ 10% × ETP50 at available; optional `--tail-amt` adds a $ floor via `max($, pct×ETP50)` | Size-scaled $ bar |

Defaults: `TAIL_PCT_DEFAULT = 0.10`, `TAIL_AMT_DEFAULT = None` (pct-only), `TAIL_THRESHOLD_MODE_DEFAULT = "and"`.

Set `--tail-pct 0` or `--tail-amt 0` to disable that leg.

### Limitations

- **Endpoint only** — avail→48hr shift; not intraday path or survival attrition.
- **Fixed floors** — no per-customer, per-lane, or hyperlocal tuning yet (`TailThresholdConfig` is the extension point).
- **Not broker-calibrated** — thresholds are DS-chosen gates, not a UX study of “meaningful” move.
- **`scaled` mode** uses ETP50 at first available snapshot as baseline; null/missing baseline → 0 (loads may over-qualify).
- **No causal claim** — problem load = large observed shift, not proof the shift was inappropriate.

## Movement Categories

1. **`ShipmentChange`** — origin, location, equip, appt (within 48hrs of pick). Exclusions vs. model signal.
2. **`DeterministicChange`** — lead time clocks (`book_2_pkup`, `avail_2_book`, etc.). Expected model signals.
3. **`RandomChange`** — market indices (DAT, CPM, fuel, …). Expected ~mean zero. Includes **catch-all** loads with no shipment/difficulty/clock signal when index columns are absent.
4. **`DifficultyOverride`** — flamed, MDE, TWIC, HRHV, and other difficulty restrictions.

### Classification priority (one label per load)

When assigning a primary category, use this order (first match wins):

`ShipmentChange` → `DifficultyOverride` → `DeterministicChange` → `RandomChange` → `Unclassified`

`Unclassified` = load missing from the Davis cache join.

### Implementation flags (`dqt.analysis_orders`)

| Flag | Category when primary |
|------|------------------------|
| `charge_inc_ind` (total_charges Δ > $50, endpoint or path increase) | ShipmentChange |
| `clhp_change_ind` (CLHP pred tot/line \|Δ\| ≥ $50 or ≥ 10% of avail baseline; path scan) | ShipmentChange |
| `equip_change_ind` (EquipmentType endpoint or load_type path change) | ShipmentChange |
| `hard_ft_inc_ind` (hard_ft Δ ≥ 1, endpoint or path) | DifficultyOverride |
| `path_taken_change_ind` (path_taken changed between checkpoints) | RandomChange (before LeadtimeChange) |
| `clocks_moved_ind` (book_2_pkup or avail_2_book \|Δ\| ≥ `CLOCK_DELTA_MIN`) | DeterministicChange |
| `index_change_ind` (DAT / fuel / lag7 CPM endpoint delta) | RandomChange |
| otherwise (Davis present) | RandomChange (catch-all residual) |

Index columns require a Davis cache built after the SQL update; older parquets still classify catch-all RandomChange.

### Clock threshold (`CLOCK_DELTA_MIN = 40`)

`clocks_moved_ind` fires when **either** endpoint delta exceeds **40 hours** (absolute) on `book_2_pkup` or `avail_2_book`. On 7–14d lead loads over avail→48hr, `book_2_pkup` typically moves **100–250+ hours**, so this threshold tags almost all stable loads as clock-moved.

The drift report embeds a **Classification QA** panel with:

- \|book_2_pkup Δ\| percentiles (P25 / P50 / P75) and % ≥ threshold
- DeterministicChange count vs clock-only count
- Loads where clocks moved but Shipment/Difficulty took priority
- RandomChange split: index-detected vs catch-all

Rebuild the report to refresh QA stats for your cohort.

### Map to feature-impact mart (Rick attribution)

| Public category | Mart cat | Example features |
|-----------------|----------|------------------|
| RandomChange | 1 — Random staged data | DAT, fuel, lag7/ra7 CPM |
| ShipmentChange + DifficultyOverride | 2 — Load features × time | total_charges, hard_ft_cnt, CLHP pred tot/line, load_type |
| DeterministicChange | 3 — Deterministic | book_2_pkup, avail_2_book |

## Features

- `total_charges`, `clhp_pred_tot_cost`, `clhp_pred_line`, equip/appt — **ShipmentChange**
- `is_flamed`, `is_hrhv`, TWIC-style restrictions — **DifficultyOverride**
- `book_2_pkup`, `avail_2_book` — **DeterministicChange**
- `DAT`, `fuel_cost`, `lag7_cpm_x_miles` — **RandomChange**

## Key takeaways (Jul 27 – Aug 21 cohort, 54,573 loads)

### 1. The dial moves ~$55 on average — consistently across targets

| Series | Avg shift | % loads moving >$35 | % loads moving >2% |
|--------|-----------|---------------------|---------------------|
| **t1** | **$55** | 53% | 57% |
| t2 | $56 | 53% | 57% |
| t3 | $57 | 53% | 56% |
| t4 | $58 | 54% | 55% |
| **p50** | **$56** | 54% | 56% |

Slider targets and model p50 move together. Higher dial targets (t3/t4) climb slightly more than t1, but they're all in the same ballpark.

### 2. Most of the ~$55 is **not** from total charges changing

| | All loads | Charges stable (Δ ≤ $50) | Charge jump (Δ > $50, ~5% of loads) |
|--|-----------|--------------------------|-------------------------------------|
| **t1 shift** | **$55** | **$48** | **$192** |
| **p50 shift** | **$56** | **$48** | **$207** |

**~$7/load** of the pooled ~$55 is associated with charge increases. The other **~$48/load** happens even when charges didn't materially change.

### 3. Difficulty jumps matter on affected loads, but are rare

Loads with `hard_ft_cnt` jumping ≥1 (~1.5% of cohort) shift **~$96** vs **~$54** otherwise — an extra **~$42 on those loads**, but only **~$1/load** pooled because so few loads are affected.

### 4. Hyperlocal is a different *kind* of load, not a driver of the climb

Hyperlocal loads shift **~$33** vs **~$62** for non-HL. That's a segment comparison, not "hyperlocal caused $X of the move."

### 5. Cohort is pre-cleaned for one big ShipmentChange

You already exclude loads where pickup appt **date** changed during the window — so location/appt-driven moves are partially controlled out.

---

## Can you attribute the ~$50 increase to these categories?

**Partially yes today. Fully no — not yet.**

| Category | Can you quantify it now? | What the notebook says |
|----------|--------------------------|------------------------|
| **ShipmentChange** | **Partially** | ~**$7/load** via `total_charges_delta > $50`. Equip/location/appt flags planned. |
| **DifficultyOverride** | **Partially** | `hard_ft_cnt` jump → **~$42 extra** on ~1.5% of loads → **~$1/load** pooled. |
| **DeterministicChange** | **Partially** | `clocks_moved_ind` / `clock_only_ind` — likely most of stable-load climb. |
| **RandomChange** | **Partially** | Index deltas when Davis cache has DAT/fuel/lag7 columns; else catch-all bucket for unexplained movers. |

### What you **can** say to leadership

> "From available to 48 hours before pickup, the slider climbs **~$55/load** (t1 and p50 are aligned). About **$7** is loads where customer total charges increased more than $50. About **$1** is difficulty overrides. Most of the rest is **expected lead-time repricing** (DeterministicChange). Residual / market-index moves land in **RandomChange** — expected ~$0 net pooled, but can spike individual loads."

### What you **shouldn't** claim yet

- A full category waterfall that sums to $55 (need feature-impact OLS)
- Causal language ("caused by") — this is associative
- Precise RandomChange $ share without refreshed Davis cache + Rick's Monday readout

---

## Rough Decomposition

```
Observed avg t1 shift:     ~$55
├── ShipmentChange/charges: ~$7   (total_charges Δ > $50)
├── DifficultyOverride:     ~$1   (hard_ft jump, rare)
├── DeterministicChange:   ~$40–45 (lead-time clocks on stable loads)
└── RandomChange:          ~$0 net pooled (residual / index noise; catch-all until indices in cache)
```

To turn RandomChange into additive DAT/fuel/CPM $ shares, refresh the Davis parquet (SQL now pulls index endpoints) and/or run `build_etp_feature_impact.py`.

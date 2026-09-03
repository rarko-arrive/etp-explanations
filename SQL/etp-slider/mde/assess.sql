-- Purpose: Map MDE events to impacted loads with leadtime + model percentile attainment.
-- Owner: ETP slider analytics
-- Params: {start_date} — lower bound on MDE event start_date_time_cst (CST)
-- Consumers: notebooks/mde.ipynb (join assess_display.sql + local enrich)
-- Perf: no displayedtarget / etp_model_logs scans (~6–15s for 3mo window).

WITH mde_impacted_loads AS (
    SELECT
        mde.market_disruption_event_list_id AS mde_id,
        mde.description AS mde_description,
        mde.start_date_time_cst AS mde_start_cst,
        mde.end_date_time_cst AS mde_end_cst,
        mde.target_1_percentile,
        mde.target_2_percentile,
        mde.target_3_percentile,
        mde.target_4_percentile,
        mde.equipment_type,
        mde.load_mode,
        mde.pu_market_name,
        mde.del_market_name,
        mde.customer_code,
        mdl.loadnumber,
        CONVERT_TIMEZONE(
            'America/Chicago', 'UTC',
            mdl.last_modified_on_cst::TIMESTAMP_NTZ
        ) AS mde_applied_utc,
        l.ship_date_coalesce AS ship_date,
        l.order_status_group,
        l.load_type,
        l.loaded_miles,
        l.carrier_shipment_charges_total AS realized_cost,
        CONVERT_TIMEZONE(
            'America/Chicago', 'UTC',
            l.available_on_first_cst::TIMESTAMP_NTZ
        ) AS made_available_utc,
        CONVERT_TIMEZONE(
            l.origin_iana_timezone_name, 'UTC',
            l.pickup_appt_latest_local::TIMESTAMP_NTZ
        ) AS pickup_appt_latest_utc
    FROM core_data.components.lod__market_disruption_event_list AS mde
    INNER JOIN core_data.components.lod__market_disruption_event_loads AS mdl
        ON mdl.market_disruption_event_list_id = mde.market_disruption_event_list_id
    INNER JOIN core_data.core.loads AS l
        ON l.loadnumber = mdl.loadnumber
    WHERE mde.start_date_time_cst >= '{start_date}'::TIMESTAMP_NTZ
)

SELECT
    mil.mde_id,
    mil.mde_description,
    mil.mde_start_cst,
    mil.mde_end_cst,
    mil.target_1_percentile,
    mil.target_2_percentile,
    mil.target_3_percentile,
    mil.target_4_percentile,
    mil.equipment_type,
    mil.load_mode,
    mil.pu_market_name,
    mil.del_market_name,
    mil.customer_code,
    mil.loadnumber,
    mil.mde_applied_utc,
    mil.ship_date,
    mil.order_status_group,
    mil.load_type,
    mil.loaded_miles,
    mil.realized_cost,
    mil.made_available_utc,
    mil.pickup_appt_latest_utc,
    DATEDIFF('hour', mil.made_available_utc, mil.pickup_appt_latest_utc) AS booking_window_hrs,
    DATEDIFF('hour', mil.mde_applied_utc, mil.pickup_appt_latest_utc) AS hrs_to_pickup_at_apply,
    CASE
        WHEN mil.pickup_appt_latest_utc IS NULL THEN NULL
        WHEN DATEDIFF('hour', mil.mde_applied_utc, mil.pickup_appt_latest_utc) <= 192 THEN 'SHORT'
        WHEN DATEDIFF('hour', mil.mde_applied_utc, mil.pickup_appt_latest_utc) <= 264 THEN 'MID'
        ELSE 'LONG'
    END AS lead_band_at_apply,
    IFF(
        DATEDIFF('hour', mil.made_available_utc, mil.pickup_appt_latest_utc) BETWEEN 168 AND 336,
        1,
        0
    ) AS sarima_paint_eligible_ind,
    IFF(ep.p10 > mil.realized_cost, 1, 0) AS hit_model_p10,
    IFF(ep.p50 > mil.realized_cost, 1, 0) AS hit_model_p50,
    ep.p10 AS model_p10,
    ep.p50 AS model_p50
FROM mde_impacted_loads AS mil
LEFT JOIN core_data.components.lod__etp_percentiles AS ep ON ep.loadnumber = mil.loadnumber

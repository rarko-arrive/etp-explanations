-- Purpose: Bulk displayedtarget history for MDE assess cohort (one scan, enrich in Python).
-- Owner: ETP slider analytics
-- Params: {start_date} — lower bound on MDE event start_date_time_cst (CST)
-- Consumers: notebooks/mde.ipynb via dqt.etp_slider.mde.enrich

WITH cohort_loads AS (
    SELECT DISTINCT mdl.loadnumber
    FROM core_data.components.lod__market_disruption_event_list AS mde
    INNER JOIN core_data.components.lod__market_disruption_event_loads AS mdl
        ON mdl.market_disruption_event_list_id = mde.market_disruption_event_list_id
    WHERE mde.start_date_time_cst >= '{start_date}'::TIMESTAMP_NTZ
)

SELECT
    dt.loadnumber,
    dt.displayid::STRING AS displayid,
    dt.etpvalue,
    dt.percentile::FLOAT AS percentile,
    dt.marketdisruptioneventlistid AS snapshot_mde_id,
    dt.modifiedon::TIMESTAMP_NTZ AS modified_ts
FROM dapl_raw.accelerateprod.lod__displayedtarget AS dt
INNER JOIN cohort_loads AS cl ON cl.loadnumber = dt.loadnumber
WHERE
    dt.displayid IN ('1', '2', '3', '4')
    AND dt.modifiedon::TIMESTAMP_NTZ >= DATEADD('day', -30, '{start_date}'::TIMESTAMP_NTZ)

-- Market Movement MDE events applied to loads (explorer timeline overlay).
-- Params: {loadnumber_in_list} — comma-separated loadnumbers (batched in Python).
-- v1: description = 'Market Movement' only (excludes HVHR, Lane Maker, experiments).

SELECT
    mdl.loadnumber,
    mdl.market_disruption_event_list_id AS mde_id,
    mdlist.description AS mde_description,
    CONVERT_TIMEZONE(
        'America/Chicago', 'UTC',
        mdl.last_modified_on_cst::TIMESTAMP_NTZ
    ) AS applied_at_utc,
    CONVERT_TIMEZONE(
        'America/Chicago', 'UTC',
        mdlist.start_date_time_cst::TIMESTAMP_NTZ
    ) AS mde_start_utc
FROM core_data.components.lod__market_disruption_event_loads mdl
INNER JOIN core_data.components.lod__market_disruption_event_list mdlist
    ON mdlist.market_disruption_event_list_id = mdl.market_disruption_event_list_id
WHERE
    mdl.loadnumber IN ({loadnumber_in_list})
    AND mdlist.description = 'Market Movement'

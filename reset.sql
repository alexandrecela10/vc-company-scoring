-- ============================================================
-- RESET SCRIPT — wipes all data, leaves schema intact.
-- Run this ONCE in Supabase SQL Editor, then re-run schema.sql (for the
-- new unique index) and seed.sql for a clean demo state.
-- ============================================================

-- TRUNCATE in dependency order, CASCADE clears everything else.
TRUNCATE TABLE
    gap_action,
    pipeline_event,
    company_metric_value,
    user_weight,
    company,
    metric,
    metric_type,
    data_source
RESTART IDENTITY CASCADE;

-- ============================================================
-- DEMO BACKDATE (run AFTER seed.sql)
-- Make GreenGrid's "Is in Growing Region" row stale so the
-- market agent has something to refresh during the demo.
-- ============================================================
-- UPDATE company_metric_value
-- SET captured_at = NOW() - INTERVAL '210 days'
-- WHERE company_id = '44444444-0000-0000-0000-000000000003'
--   AND metric_id  = '33333333-0000-0000-0000-000000000008'
--   AND is_latest  = TRUE;

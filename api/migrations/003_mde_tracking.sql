-- Adds minimum-detectable-effect tracking to the rolling analysis snapshots,
-- so the dashboard/API can show "at this sample size, you can detect an
-- effect of at least X%" alongside the p-value trend, per the spec's
-- "p-value and minimum-detectable-effect tracking" requirement.

ALTER TABLE experiment_analysis_snapshots
    ADD COLUMN min_detectable_effect NUMERIC;

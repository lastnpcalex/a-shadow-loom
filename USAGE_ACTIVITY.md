# Usage Activity

The Admin Usage tab is owned by `usage_activity.py`, `usage_activity_store.py`,
and `static/admin/admin-activity.js/css`. Main mounts its router and runs a
managed background refresh every minute. Admin proxies these endpoints:

- `GET /api/usage/activity`: periods, 60 calendar days, provider totals,
  activity averages, schedule, and the latest 31 saved daily reports.
- `POST /api/usage/snapshots/yesterday`: create or retrieve yesterday's report.
- `PUT /api/usage/settings`: timezone (IANA name), hour (0–23), enabled (boolean).
- `GET /api/usage/chart`: period (`max`, `year`, `month`, `week`, `day`, `custom`),
  custom start/end ISO dates, group (`harness` / `provider`), and selection
  (`all` or a category returned by the chart). Invalid ranges return HTTP 400.

Token history has period, breakdown, category, and color controls. Year means
the last twelve calendar months through now, month the last calendar month through now, week the last
seven calendar dates, and day today so far. Custom end dates are inclusive in
the selected reporting timezone. Max includes all retained history. Hourly
buckets show up to two dates, daily buckets up to 90, and monthly buckets longer
ranges. Empty buckets remain visible. Hover or focus bars for counts and category
details; the matching breakdown table covers the selected range. The original
period cards and all-time provider table remain independent of chart filters.

Harness and backend are independent: Goose using llama-server is Goose in the
harness view and Llama Server in the backend view. The ledger adds one harness
label per message, refreshed from turn-level evidence. Unknown and ambiguous
historical harnesses remain unlabeled, including deleted local rows whose backend
label alone cannot establish their harness. Saved daily reports remain unchanged.

Default schedule: 07:00 America/Los_Angeles. Main catches up missed dates,
seven per refresh, after downtime. On first installation it starts with the
latest due day rather than generating reports for the entire historical archive.
Timezone changes use a separate report identity; existing reports remain stored
in their original timezone. The current tab lists reports for the selected zone.

SQLite storage is additive and contains no prompts, responses, or tool content:

- `usage_activity_footer`: one row per assistant message, only its ID,
  creation timestamp, backend/provider label, harness label, and input/output counts. Refresh replaces
  counters by ID; it never adds cumulative values repeatedly. Conversation and
  branch deletion capture final counters in the same transaction before removal.
- `usage_activity_reports`: immutable JSON daily summaries including provider
  totals and their Markdown export. Dates and generation timestamps are separate.
- `usage_activity_settings`: a single schedule row.
- `usage_activity_schedule_cursor`: one row per reporting timezone. Manual
  reports do not advance it or cause missed automatic dates to be skipped.

All dashboard totals come from the retained footer rows, so live and saved
history are never added together. Daily snapshots are a frozen view; late usage
updates affect dashboard totals but do not rewrite a saved report. Deletions
before installation and previously missing telemetry cannot be reconstructed.
Deleting the whole database also removes its activity history.

Rolling five hours means 18,000 elapsed seconds, independent of DST. Calendar
periods use timezone rules; comparisons use the equivalent local date/time in
the preceding period, clamping day numbers for shorter months and leap years.
Missing counters display as unavailable. Totals with partial coverage sum only
known counters. Empty calendar dates remain visible on the chart.

These are activity figures from chat footers, not provider billing consumption.
The Llama Server provider row combines identifiable local calls across Weave,
local Claude Code, and Goose. Turn-level model-provider evidence takes priority;
historical local model selectors are a fallback. Refresh reclassifies existing
retained rows without adding counts twice. Deleted records that were retained
before this classifier and no longer have model evidence remain under their
original label. Saved reports remain immutable. Internal search, compaction,
and other calls without saved chat-footer counters are outside these totals.
Claude input is final context size, not total input across tool cycles. Child
agent calls are not counted. All generated message branches count; copied fork
messages currently have no footer counters and can lower coverage / inflate turn
counts. The ledger does not change generation accounting.

Validation uses isolated SQLite fixtures and headless Chromium with the real
Admin DOM and modular API, including Markdown/JSON downloads. No test restarts
or writes to a live Loom instance.

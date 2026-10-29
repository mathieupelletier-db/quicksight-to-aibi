# Migrating BI reports to Databricks AI/BI: a playbook

A tool-neutral approach for moving Power BI, Tableau and QuickSight reports to AI/BI dashboards
and Genie on a Unity Catalog semantic layer. It is written for the people planning the migration
(data platform owners, BI leads, solution architects). The `quicksight-to-aibi` skill in this
repo automates the QuickSight conversion step.

## 1. Principles

- **Consolidate on one governed semantic layer.** Define measures once, as Unity Catalog metric views, and reuse them in dashboards, Genie and any remaining BI tool. The payoff is fewer tools and consistent numbers, not a one-for-one copy of every report.
- **Migrate what is used.** Most estates carry many reports nobody opens. Usage data decides what moves, what retires, and in which order.
- **Prove the numbers.** Reconcile every migrated KPI against the source before users switch. One unexplained difference costs more trust than a list of known gaps.
- **Be explicit about what stays.** Some report types (pixel-perfect paginated reports, heavy custom visuals) are better left where they are, at least at first. Say so at the start.

## 2. Inventory and usage

Collect per report: id, name, owner/workspace, semantic model or dataset, view count, distinct viewers, report type, whether it's shared externally, and whether its data is already in Unity Catalog.

| Source | Inventory | Usage |
|---|---|---|
| Power BI | Admin REST API: `GET /admin/groups?$expand=reports` (page with `$top`/`$skip`) | Admin activity events (`ViewReport`), which only go back about 30 days. For 90 days, use the tenant audit-log export |
| Tableau | Metadata / REST API (workbooks, data sources) | Repository / admin views |
| QuickSight | `aws quicksight list-dashboards` + `describe-dashboard-definition`, or an asset-bundle export | CloudWatch `AWS/QuickSight` `ViewCount` by `DashboardId` (verify the metric name in your account) |
| Databricks SQL | `system.query.history` | Client split, and the tables each client reads |

Two traps:
- **Missing usage is not zero usage.** A report absent from a 30-day activity window may be a quarter-end report. Treat it as "unknown" and never retire it on that basis.
- **"Unknown client" is usually the biggest bucket.** Account-level telemetry often can't attribute most queries to a BI tool. Run the split inside the workspace:

```sql
-- Which clients query the warehouse (check the actual client_application spellings first)
SELECT date_trunc('month', start_time) AS month,
       coalesce(client_application, 'unknown') AS client,
       CASE WHEN query_source.dashboard_id   IS NOT NULL THEN 'aibi_dashboard'
            WHEN query_source.genie_space_id IS NOT NULL THEN 'genie' ELSE 'other' END AS surface,
       count(*) AS queries, count(DISTINCT executed_by) AS identities,
       round(sum(read_bytes) / 1e12, 2) AS tb_read
FROM system.query.history
WHERE start_time >= current_date() - INTERVAL 180 DAYS
GROUP BY ALL ORDER BY month, queries DESC;

-- Which tables a BI tool reads (scopes the metric views per domain)
SELECT t.source_table_catalog, t.source_table_schema, t.source_table_name,
       count(DISTINCT q.statement_id) AS queries
FROM system.query.history q
JOIN system.access.table_lineage t ON t.statement_id = q.statement_id
WHERE q.start_time >= current_date() - INTERVAL 30 DAYS
  AND t.event_date >= current_date() - INTERVAL 30 DAYS
  AND lower(q.client_application) LIKE '%power%bi%'
  AND t.source_table_name IS NOT NULL
GROUP BY ALL ORDER BY queries DESC LIMIT 100;
```

## 3. Disposition rules

Apply in order; the first match wins.

1. Usage unknown → **needs usage**. Never retire on missing data.
2. No views in 90 days → **retire**.
3. Paginated / pixel-perfect report → **retain** in the source tool for now.
4. Shared externally (partners, customers) → **retain pending**, until external sharing/embedding is validated.
5. Source data not reachable from Unity Catalog → **blocked: data**. Bridge it first (federation or ingestion).
6. High complexity (advanced DAX/LOD, table calculations, custom visuals, drill-down) → **wave 2**.
7. Everything else → **wave 1**, ranked by distinct viewers.

Group waves by semantic model or dataset, so each metric view is built once for all the reports that share it.

## 4. Conversion paths

| Source | Path |
|---|---|
| Power BI | Genie Code `/importBI` on `.pbit`/`.pbir` files, one report per session. Upload a shared semantic model's `.pbit` alongside, add screenshots of the original, and connect the live source where available for validation. |
| Tableau | Genie Code `/importBI` on `.twb(x)`, with published data sources as `.tds`. |
| QuickSight | Asset-bundle export → this repo's `quicksight-to-aibi` skill (parse, translate, rebuild, validate). |

In every case the output is metric view definitions plus an AI/BI dashboard. Expect to iterate: migration is assisted, not one-click.

## 5. Pilot

- **Scope:** 3–5 reports of varying complexity from wave 1, including at least one with row-level security.
- **Acceptance criteria:**
  - every KPI tile and one point per chart reconcile within 0.5% at the same filters, or the difference is explained
  - a restricted test persona sees only their rows and masked columns
  - business sign-off on look and feel
  - pilot users actually use it: a minimum number of weekly users for two consecutive weeks
- **Timebox:** about 4–6 weeks, including 2 weeks of buffer for feedback. Set the dates against the decision the pilot feeds, such as a license renewal or a platform review.

## 6. Security parity

Move security from the BI tool into Unity Catalog, so the same rules apply in dashboards, Genie, SQL and any other tool:

- **RLS rules** (Power BI roles, Tableau user filters, QuickSight permission datasets) → an **entitlements table** plus a **row filter function** that checks `current_user()` or `is_account_group_member(...)`. Users with no entitlement see nothing.
- **Column-level rules** → **column masks**: NULL, last-4 digits, or a hash when `count(DISTINCT)` must stay exact.
- Publish dashboards **without embedded credentials**, so each viewer queries as themselves.
- Test with a restricted persona. Check that the visible slice equals the user's entitlement (not just "one value"), and that masked columns match the mask pattern.

## 7. Known gaps to set expectations on

Re-check these against current AI/BI release notes. They change.

- Expand/collapse pivots and drill-down.
- Some visual types (gauge, treemap, word cloud, radar); use Vega-Lite custom visualizations or a sorted bar instead.
- Dynamic Top-N as a widget setting; use ranked SQL.
- Calculations that depend on a parameter *before* aggregation; use dashboard dataset parameters, because metric views don't take parameters.
- Advanced DAX filter-context functions and LOD expressions; these need hand translation.
- Exact number/currency formats.
- Link-outs from table cells to external systems.

## 8. Adoption and decommissioning

```sql
-- Weekly AI/BI dashboard and Genie users (list the action names first; editors also emit events)
SELECT date_trunc('week', event_date) AS week, service_name,
       count(DISTINCT user_identity.email) AS users, count(*) AS events
FROM system.access.audit
WHERE service_name IN ('dashboards', 'aibiGenie') AND event_date >= current_date() - 90
GROUP BY ALL ORDER BY week, service_name;
```

Gates:
- **Pilot → wave 1:** reconciliation and persona tests pass, plus sign-off and adoption.
- **Source report read-only:** its views fall to near zero for 4 weeks after its replacement goes live.
- **Decommission a tool:** time it to the tool's renewal date. Retain only what the disposition rules keep.

## 9. Risks

| Risk | Mitigation |
|---|---|
| The team that owns the migration is also busy with other platform work | Pilot small; use partner or vendor migration help for later waves |
| Competing platform pitches during the evaluation | Lead with governance, Genie and one semantic layer usable by every tool |
| Identity differs across tenants/clouds (users can't sign in where the dashboards live) | Confirm identity and group mapping before the pilot |
| Paginated-report expectations | State "retain" up front, in the pilot charter |
| Period-over-period numbers on an unfinished period look like a collapse | Flag it in reconciliation; offer like-for-like comparisons |

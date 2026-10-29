---
name: quicksight-to-aibi
description: Migrate Amazon QuickSight dashboards, analyses and Q topics to Databricks AI/BI dashboards on Unity Catalog metric views, with Genie spaces. This is the QuickSight counterpart of Genie Code /importBI, which only covers Power BI and Tableau. Use it whenever someone wants to move, convert, replicate, rebuild, export or assess QuickSight reports on Databricks. Triggers include a .qs asset bundle, QuickSight dashboard/dataset JSON, exporting dashboards from a QuickSight account, a QuickSight-to-AI/BI effort estimate, a "QuickSight takeout", moving QuickSight Q topics to Genie, or mapping QuickSight RLS/CLS, SPICE datasets or calculated fields to Databricks. It also covers read-only migration assessments (complexity, gaps, effort) before anything is built.
---

# QuickSight → Databricks AI/BI

Turns a QuickSight asset bundle into:
- Unity Catalog metric views (the governed measures)
- an AI/BI dashboard (`.lvdash.json`) wired to them
- UC row filters / column masks that replace QuickSight RLS/CLS
- optionally a Genie space that replaces a Q topic
- a migration report listing everything that didn't translate 1:1

The bundled scripts cover the deterministic parts: exporting, parsing, translating calculations,
mapping visuals and layout, and finding gaps. Your job is the judgement: where each calculation
lives, how to handle each gap, and proving the numbers match.

Related skills, loaded when you reach their step:
- `databricks-core`: CLI auth and profile selection. Never pick a profile yourself; ask.
- `databricks-metric-views`: metric view YAML.
- `databricks-aibi-dashboards`: `.lvdash.json` structure and widget specs. It's the source of truth for widget JSON, and it requires testing every query.
- `databricks-genie-agents`: Genie spaces.
- `databricks-dabs`: packaging and deploying.

## Ground rules
- **Read-only by default.** Exporting, parsing, assessing, read-only SQL (`SELECT`, `DESCRIBE`, `SHOW`, `information_schema`) and generating files need no approval. Ask first before creating or changing anything in a workspace: connections, catalogs, views, metric views, functions, row filters, Genie spaces, dashboards. These are shared objects other people depend on.
- **No customer data leaves their environment.** Bundles contain definitions only. Keep outputs in the user's working folder.
- **Never silently drop or change something.** Every measure, filter and row exclusion either carries over or appears in the report with a workaround. People compare the new dashboard against the old one tile by tile, so a quietly changed number destroys trust faster than an honest gap list.
- **The output files are the deliverable.** Write `MIGRATION_REPORT.md` (or `ASSESSMENT.md`) even if you'd normally avoid creating report files. The user asked for them and reviews them.

## Workflow

Default output folder: `qs2aibi_out/<dashboard-slug>/`, unless the user names one.

### 1. Get the bundle
If the user already has a `.qs` file, a folder of JSON, or `describe-*` output, use it. Otherwise export from QuickSight. This needs AWS CLI v2 credentials with QuickSight list/read and asset-bundle export permissions on every exported asset; ask which AWS profile to use.
```bash
# named dashboards -> one bundle
python3 <skill>/scripts/export_bundle.py --account-id <acct> --region <region> [--profile <p>] \
    --dashboard-id <id> [--dashboard-id <id2>] -o qs2aibi_out/bundle.qs
# discover (optionally by name) and export in batches -> qs2aibi_out/bundles/bundle-001.qs ...
python3 <skill>/scripts/export_bundle.py --account-id <acct> --region <region> [--profile <p>] \
    --all [--name-like sales] --batch-size 20 -o qs2aibi_out/bundles/
```
It exports `QUICKSIGHT_JSON` with all dependencies (datasets, data sources, themes, topics). Exporting is read-only on the QuickSight side.

### 2. Parse
```bash
python3 <skill>/scripts/parse_bundle.py <bundle.qs|dir|*.json> -o <out>/spec.json --summary <out>/summary.md
```
Read `summary.md` first. It covers:
- **Visuals:** QuickSight type → AI/BI widget, with dimensions and measures, per sheet.
- **Datasets:** sources, joins, import mode and security.
- **Rows silently dropped:** dataset filters and custom-SQL `WHERE` clauses. Reproduce them, or numbers won't tie.
- **Defined but unused fields:** don't migrate dead logic; list it for the user.
- **Q topics, themes, SPICE refresh schedules.**
- **A gap table with a complexity rating.** It flags table calculations, parameters inside calculations, non-exact filter matches (`CONTAINS`), hard-coded filter ranges, Top-N, visuals without an equivalent, actions and security.

`spec.json` has the detail: translated SQL for every calculation with `needs_review` notes, field wells (e.g. `BarValues` vs `LineValues`), suggested widget positions, controls and parameters.

The parser classifies JSON by content, so folder names inside the bundle don't matter. If it finds no dashboard, check `skipped_files` before assuming the bundle is broken.

**If the user asked for an assessment only**, also do step 3's read-only checks when a workspace is available, then write `ASSESSMENT.md` (format below) and stop.

### 3. Look at the target before designing (read-only)
This step prevents two expensive mistakes: duplicating a semantic layer that already exists, and misreading data that's already filtered for you.
- **Existing metric views:** `SELECT table_name FROM <catalog>.information_schema.tables WHERE table_schema = '<schema>' AND table_type = 'METRIC_VIEW'`. If one already covers a dataset's measures, reuse or extend it and say so, rather than creating a parallel definition.
- **Existing security:** query `information_schema.row_filters` and `information_schema.column_masks` for the mapped tables. If a row filter is active, every number you validate is filtered for *your* identity. Say so next to each validated number, and don't read restricted data as "the table only contains X".
- **Data reality:** min/max of the main date column and row counts. A partial last period (a week or year still in progress) makes period-over-period comparisons misleading. Flag it in the report even though QuickSight showed the same thing.

### 4. Map data to Unity Catalog
- Every dataset source needs a UC table or view. Ask for the target catalog/schema if they weren't given.
- Record the mapping in `<out>/mapping.json` and verify it read-only: the table exists, the columns exist, row counts are plausible.
- Rewrite custom SQL against UC names and test it with `LIMIT`.
- Recreate dataset joins and renames (`datasets[].joins` / `renames`) in the source view, or in the metric view `joins`.
- SPICE datasets point at their underlying source.

See `references/data-bridge.md`, including federation for Redshift/Athena when nothing is in UC yet.

### 5. Design the semantic layer
Aim for **one metric view per QuickSight dataset**, unless step 3 found one to reuse:
- `source`: the mapped table or view (fully qualified). Dataset filters (`row_drops` of kind "dataset filter") go in the metric view `filter`, or in the source view.
- `dimensions`: every column used as a visual dimension or in a filter, plus scalar calculated columns.
- `measures`: every aggregated visual field and aggregate calculated field. The SQL is already in `measures[].sql` / `calculated_fields[].sql`. Ratios stay ratios of aggregates.

Keep a calculation **out** of the metric view, in dashboard dataset SQL, when:
- it uses a QuickSight parameter (metric views can't take parameters; keep a named parameter `:Param`)
- it is a table calculation (`percentOfTotal`, `rank`, `periodOverPeriod*`, `runningSum` …); use window functions
- a user-controlled filter must apply *before* aggregation
- a filter needs non-exact matching (`CONTAINS`/`STARTS_WITH`); use a `LIKE` condition with a parameter

Document each exception in the report. `references/expression-mapping.md` has the placement rules and the hand translations for everything in `needs_review`.

Write each metric view as `<out>/metric_views/<name>.sql`, following `databricks-metric-views`.

### 6. Build the dashboard
Follow `databricks-aibi-dashboards` for the JSON:
- one page per QuickSight sheet
- each visual → its `aibi_widget` at its `position` on the 12-column grid (`"layoutVersion": "GRID_V1"` on every page; positions are approximate, so re-pack rows to fill width 12 with no overlaps)
- combo `LineValues` → `y.secondary`
- each control → the filter widget in `references/visual-mapping.md`; parameter controls → dataset parameters
- datasets query the metric views with `MEASURE(...)`
- use the fallback in `references/visual-mapping.md` where there's no native widget, keeping the visual's title
- `themes[].data_colors` can seed the dashboard palette

Write `<out>/dashboards/<slug>.lvdash.json`.

### 7. Security
For `security.row_level` / `column_level` / `tag_rls`, write `<out>/security/security.sql` per `references/security.md`:
- an entitlements table built from the RLS rules dataset. The spec gives `principal_columns` (UserName/GroupName) and `rule_columns`.
- row filter functions
- column masks

Reconcile it with any security found in step 3, and state that dashboards must be published without embedded credentials.

### 8. Genie (when the bundle has a Q topic, or the user wants natural-language Q&A)
Write `<out>/genie/space_plan.md` per `references/genie-topics.md`: the metric views to include, a glossary from the topic synonyms, instructions, sample questions and benchmark SQL. Creating the space is a workspace write, so ask first.

### 9. Validate (read-only)
Run every dashboard dataset query on the user's warehouse (Statement Execution API or `databricks` CLI). Check each returns rows and that its column names match the widget fields.
- **Metric view already exists:** run the real `MEASURE()` queries against it.
- **Not yet created:** validate each measure with `SELECT <dims>, <measure exprs> FROM <source> GROUP BY ALL`, and say plainly that `MEASURE()` hasn't been tested.

Log every query, its result, and the identity it ran as in `<out>/validation.md`. A failing query blocks deployment; fix it or list it as a gap.

### 10. Deploy and reconcile (only with approval)
- Package with `databricks-dabs`: the dashboard, plus a job or SQL task for the metric views and security. Deploy only once the user says so.
- Reconcile against QuickSight: for each KPI and one point per chart, at the same filters, write `<out>/reconciliation.csv` with `visual,measure,filters,quicksight_value,aibi_value,pct_diff`. Explain anything over 0.5% (definition, timing, SPICE staleness, RLS identity) before sign-off.

### 11. Report
Write `<out>/MIGRATION_REPORT.md`:

```markdown
# <Dashboard> — QuickSight → AI/BI migration
## Summary
Complexity; visuals native / fallback / not migrated; datasets → metric views (new or reused); status (generated / validated / deployed / reconciled).
## Data mapping
| QuickSight source | Unity Catalog | Verified |   + joins/renames, rows dropped and where that's reproduced
## Semantic layer
Metric views + measures; calculations kept in dashboard datasets and why; unused fields not migrated.
## Visuals
| Sheet | Visual | QuickSight type | AI/BI widget | Notes |
## Filters and parameters
Each filter/control → AI/BI equivalent; match-operator or hard-coded-range changes.
## Security
RLS/CLS → UC row filters/masks (new or existing); publish without embedded credentials.
## Genie
Topic → space plan, or recommendation.
## Data findings
Partial periods, unexpected distributions, identity-filtered validation.
## Gaps and workarounds
| Severity | Item | Workaround | Needs decision? |
## Validation
Queries run, identity, results; MEASURE() tested or not; reconciliation status.
## Next steps / decisions for the user
```

## Assessment format
For `ASSESSMENT.md`:
- **Per dashboard:**
  - complexity, with the reason
  - visual inventory: native / fallback / none
  - datasets, sources and joins
  - security
  - rows dropped and unused fields
  - the gap table
- **Data findings** from step 3, if a workspace was available (e.g. partial last period).
- **Effort** as a rough range per dashboard, not a promise. Low ≈ 0.5–1 day, medium ≈ 1–3 days, high ≈ 3–5+ days, including validation and reconciliation. Name the drivers: table calcs, RLS identity mapping, custom SQL, data not yet in UC.
- **Decisions the customer must make:**
  - the data bridge
  - identity mapping for RLS
  - accepting fallbacks and filter-behaviour changes
  - dropping unused fields
  - Genie

## Lessons from real migrations
- **Ratios stay measures.** Never store a percentage as a column, or filtered totals will be wrong.
- **Distinct-count KPIs break under pre-aggregation.** Keep them on row-level data.
- **Filters that trim rows before aggregation** (time windows, minimum quantities) need a parameterised dataset, not a field filter.
- **Expand/collapse pivots don't exist in AI/BI.** Offer a pivot plus a detail table, and say so up front.
- **Number and currency formats:** check them in the widget encodings and note any substitutes.
- **Period-over-period on an unfinished period** reads as a collapse. Flag it and offer a like-for-like version.

# quicksight-to-aibi

A [Claude Code](https://docs.claude.com/en/docs/claude-code) skill that migrates **Amazon QuickSight** dashboards to **Databricks AI/BI dashboards** built on **Unity Catalog metric views**. It also carries QuickSight row- and column-level security over to Unity Catalog row filters and column masks, and turns QuickSight Q topics into a **Genie** space plan.

Databricks' Genie Code `/importBI` converts Power BI and Tableau files. QuickSight has no report file to import, so this skill works from QuickSight **asset bundles** (`.qs`) instead.

## What it does

| Step | How |
|---|---|
| Export | `scripts/export_bundle.py` runs QuickSight asset-bundle export jobs (`QUICKSIGHT_JSON`, all dependencies), for named dashboards or for every dashboard matching a name, in batches |
| Parse | `scripts/parse_bundle.py` turns a bundle into `spec.json` and a readable `summary.md`: datasets, joins, calculated fields, visuals, filters, parameters, security, Q topics, themes, refresh schedules |
| Translate | `scripts/qs_expr.py` translates QuickSight calculations to Databricks SQL. Anything it can't translate faithfully is flagged, never dropped |
| Map visuals | Each QuickSight visual maps to an AI/BI widget type with a suggested grid position, and gets a fallback where there's no native equivalent |
| Find gaps | Table calculations, parameters used inside calculations, non-exact filter matches, hard-coded filter ranges, rows silently dropped by dataset filters, unused fields, RLS/CLS, SPICE. Each comes with a complexity rating |
| Build (guided) | The skill walks Claude through mapping the data to Unity Catalog, designing metric views, building the `.lvdash.json`, writing the security SQL and a Genie plan, read-only validation, deployment (with approval) and reconciliation |
| Report | `MIGRATION_REPORT.md`, or `ASSESSMENT.md` for a read-only assessment with effort ranges and the decisions the customer needs to make |

The deterministic parts (export, parsing, translation, mapping) are plain Python. The judgement calls (where a calculation should live, how to handle each gap, proving the numbers match) are left to the agent, with guidance in `SKILL.md` and `references/`.

## Install

```bash
git clone https://github.com/mathieupelletier-db/quicksight-to-aibi ~/.claude/skills/quicksight-to-aibi
```

Or clone it into a project's `.claude/skills/` folder. Claude Code loads the skill from `SKILL.md` and uses it when you ask about QuickSight migrations.

Recommended companion skills: the `databricks` plugin from the Claude Code plugin marketplace (`databricks-core`, `databricks-metric-views`, `databricks-aibi-dashboards`, `databricks-genie-agents`, `databricks-dabs`). This skill defers to them for the exact metric view YAML, dashboard JSON and deployment formats.

## Requirements

- Python 3.10+. The scripts use only the standard library; `pytest` is needed for the tests.
- For export: AWS CLI v2 with credentials that can list and read the dashboards, datasets, data sources, themes and topics, and run asset-bundle export jobs.
- For validation: the Databricks CLI with a configured profile and a SQL warehouse.

## Usage

Ask Claude Code in plain language, for example:

- "Export the *Sales Overview* QuickSight dashboard from account 123456789012 and convert it to AI/BI. The Redshift tables are in `main.sales` now. Read-only for now."
- "Here's a QuickSight export, `bundles/finance.qs`. Give me a read-only migration assessment: complexity, what won't translate, effort per dashboard."
- "This dashboard uses QuickSight RLS by region. Carry it over to Unity Catalog."

Or run the scripts directly:

```bash
python3 scripts/export_bundle.py --account-id 123456789012 --region us-east-1 --profile my-aws \
    --all --name-like sales -o bundles/
python3 scripts/parse_bundle.py bundles/bundle-001.qs -o out/spec.json --summary out/summary.md
```

## Safety

- **Read-only by default.** Exporting, parsing, generating files and read-only SQL need no approval. The skill tells Claude to ask before creating connections, views, metric views, functions, row filters, Genie spaces or dashboards.
- **Definitions only.** Asset bundles contain definitions only, never data or data-source passwords. Outputs stay in your working folder.
- **Viewer credentials.** Dashboards should be published without embedded credentials, so that row filters and masks apply to each viewer.

## What doesn't translate 1:1

The skill flags each of these with a workaround:
- table calculations (`percentOfTotal`, `periodOverPeriod*`, `rank`, …), rebuilt as window SQL
- parameters used inside calculations; metric views can't take parameters, so these become dashboard dataset parameters
- gauges, word clouds, treemaps and radar charts (Vega-Lite or a bar chart instead)
- expand/collapse pivots
- URL actions (rebuilt as link columns)
- tag-based RLS for embedding
- SPICE datasets: AI/BI queries the source live

See `references/` for the mapping tables.

## Tests and evals

```bash
python3 evals/fixtures/build_fixtures.py   # builds two synthetic .qs bundles over samples.bakehouse
python3 -m pytest scripts/tests -q
```

`evals/evals.json` holds three end-to-end test prompts with assertions, in the format used by Anthropic's skill-creator. The fixtures describe reports over the `samples.bakehouse` sample dataset that ships with every Databricks workspace, so anyone can rerun them.

## Status and limitations

- Early version. The parser follows the QuickSight API's documented `Definition` / `DataSet` / `DataSource` / `Topic` / `Theme` shapes, and has been tested on synthetic bundles only, **not yet on a real QuickSight export**. Please open an issue if a real bundle trips it up.
- Grid positions are approximate. Generated dashboards should be checked in the browser before publishing.
- Planned:
  - generate window SQL for common table calculations, instead of only flagging them
  - profile static filter values and thresholds against the data
  - check that URL-action placeholders match the visual's fields

## Also in this repo

[`docs/migration-playbook.md`](docs/migration-playbook.md) is a tool-neutral playbook for planning a BI migration (Power BI, Tableau, QuickSight) to Databricks AI/BI. It covers inventory and usage, disposition rules, pilots, security parity, reconciliation and decommissioning.

## Disclaimer

This is a community project, not an official Databricks or AWS product, and is provided as-is under the Apache 2.0 license. Validate the generated assets before using them in production.

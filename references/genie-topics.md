# QuickSight Q topics → Genie space

A QuickSight Q topic is a natural-language layer over datasets. Its Genie equivalent is a space
over the new metric views. `spec.json` → `topics[]` has everything you need: datasets, columns
with friendly names, synonyms and descriptions, the role (dimension/measure), the default
aggregation, the `included` flag, and the topic's calculated fields (translated to SQL).

## Mapping

| Topic element | Genie / Unity Catalog |
|---|---|
| Topic name / description | Genie space title / description |
| Datasets | the metric views (and any dashboard-only views) that replace them |
| `ColumnFriendlyName` | metric view dimension/measure **name** (or `display_name` if your metric view version supports it); otherwise a column comment |
| `ColumnSynonyms` | metric view `synonyms:` where supported, plus a line in the space instructions ("'sales', 'turnover' mean Revenue") |
| `ColumnDescription` | metric view `comment:` on the dimension/measure |
| `IsIncludedInTopic: false` | leave the column out of the metric view, or out of the space (often PII; check it's masked anyway) |
| Calculated fields | metric view measures (same placement rules as `expression-mapping.md`) |
| Named entities / filters | sample questions plus instructions, or a filtered view |

## Build steps
1. Create the Genie space on the metric views. Use the `databricks-genie-agents` skill for the API and space format. Creating a space is a workspace write, so ask first.
2. Instructions: a short glossary from the synonyms, plus business rules from dataset filters (`row_drops`) so Genie knows which rows are excluded.
3. Sample questions: 5–10 questions users asked of Q. Ask the user for real ones; otherwise derive them from the dashboard visuals ("revenue by product last week").
4. Benchmarks: for each sample question, the expected SQL against the metric view (`MEASURE()`), checked on the warehouse.
5. Link the dashboard to the space ("Ask Genie") so users go from the report to questions.

Natural-language Q&A is often a key requirement in these migrations. Offer a Genie space over the new metric views even when the bundle has no topic.

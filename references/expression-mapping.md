# QuickSight calculations → Databricks SQL

`scripts/qs_expr.py` translates expressions automatically. It reports what it couldn't handle in
`needs_review`. This file covers deciding **where** each calculation lives, and the hand
translations.

## Where a calculation goes

| Kind of calculation | Put it in | Why |
|---|---|---|
| Dataset calculated column (`CreateColumnsOperation`), scalar | metric view `dimensions[].expr`, or the source view | Row-level, so it belongs with the data |
| Dataset `FilterOperation` | metric view `filter:` (or the source view WHERE) | Applied before any aggregation |
| Analysis calculated field, aggregate (`sum/…`, `distinct_count`, `sumIf`) | metric view `measures[].expr` | One governed definition, reused by Genie and other tools |
| Ratio of aggregates (`sum(a)/sum(b)`) | metric view measure. Never pre-aggregate the parts | Ratios must be computed after the dashboard's filters |
| Uses a parameter (`${Param}`) inside the calc | dashboard dataset SQL with a named parameter `:Param` | Metric views can't take parameters |
| Table calculation / level-aware (`sumOver`, `percentOfTotal`, `rank`, `runningSum`, `periodOverPeriod*`, `periodToDate*`) | dashboard dataset SQL with window functions, or a metric-view window measure when it's a simple trailing/cumulative window | See patterns below; verify the numbers |
| Filter that must apply **before** aggregation and is user-controlled | dashboard dataset SQL with a parameter | A field filter on a metric view applies after the measure's grain is fixed |

## Auto-translated (checked by tests)
- **Conditional:** `ifelse` → `CASE WHEN`. `isNull`/`isNotNull` → `IS [NOT] NULL`. `coalesce`, `nullIf`.
- **Aggregates:**
  - `sum/avg/min/max/count/median/stdev/var` translate directly.
  - `distinct_count(x)` → `count(DISTINCT x)`.
  - `sumIf(x, c)` → `sum(CASE WHEN c THEN x END)`, and the same for `avgIf`, `countIf`, `minIf`, `maxIf` and `distinct_countIf`.
  - `percentile*` → `percentile_cont`/`percentile_disc`.
- **Dates:**
  - `dateDiff(a, b, 'DD')` → `timestampdiff(DAY, a, b)`.
  - `truncDate('MM', d)` → `date_trunc('MONTH', d)`.
  - `addDateTime(n, 'DD', d)` → `timestampadd(DAY, n, d)`.
  - `extract('YYYY', d)` → `extract(YEAR FROM d)`.
  - `now()` → `current_timestamp()`. `formatDate` → `date_format`.
  - `parseDate` → `to_timestamp`, flagged: check the pattern letters.
- **Strings:**
  - `concat`, `left`, `right`, `substring` and `trim` translate directly.
  - `strlen` → `length`. `toLower`/`toUpper` → `lower`/`upper`. `split` → `split_part`. `replace`.
  - `locate(s, sub)` → `locate(sub, s)`: the argument order is swapped.
  - `contains(s, sub, CASE_INSENSITIVE)` → `contains(lower(s), lower(sub))`.
- **Casts and math:**
  - `toString` → `CAST(… AS STRING)`. `parseInt`/`decimalToInt` → `INT`. `parseDecimal`/`intToDecimal` → `DOUBLE`.
  - `log` → `log10` (QuickSight `log` is base 10). `ln`, `exp`, `sqrt`, `mod`, `round`, `ceil`, `floor` and `abs` translate directly.
- **References:**
  - Fields `{Name}` → column (backticked when the name isn't a plain identifier).
  - A calculated field that references another is inlined.
  - Parameters `${P}` → `:P`.

## Table-calculation patterns (hand translation)
Put these in the dashboard dataset SQL (a CTE over the metric view or the table):

- **`percentOfTotal(sum(x), [p])`**: `sum(x) / sum(sum(x)) OVER (PARTITION BY p)`
- **`sumOver(sum(x), [p])`**: `sum(sum(x)) OVER (PARTITION BY p)`
- **`runningSum(sum(x), [d ASC], [p])`**: `sum(sum(x)) OVER (PARTITION BY p ORDER BY d ROWS UNBOUNDED PRECEDING)`
- **`rank([sum(x) DESC], [p])`**: `rank() OVER (PARTITION BY p ORDER BY sum(x) DESC)`
- **`difference(sum(x), [d ASC], -1, [p])`**: `sum(x) - lag(sum(x)) OVER (PARTITION BY p ORDER BY d)`
- **`periodOverPeriodPercentDifference(sum(x), d, YEAR, 1)`** (YoY):
  - Self-join or `lag` on the yearly aggregate: `(cur - prev) / prev`.
  - If the dashboard filters on year, compute both years inside the dataset with the year as a parameter. Otherwise filtering removes the prior year and YoY goes to NULL/0.
- **`periodToDateSum(x, d, MONTH)`**: `sum(x)` with `WHERE d >= date_trunc('MONTH', <as-of>)`, where the as-of date is a parameter or `current_date()`.

Metric-view window measures can express simple trailing or cumulative windows over a date dimension. Use them only when the window is fixed and no date filter is applied to the same dimension. Check the `databricks-metric-views` skill for the current `window:` syntax and its limits.

## QuickSight aggregation behaviour to keep in mind
- `AggregationFunction` on a visual field defaults to SUM for numbers and COUNT for strings. The parser writes it into `measures[].sql`.
- `ifelse` on aggregates inside a visual is evaluated post-aggregation. Keep it as a measure, not as a dimension.
- QuickSight divides integers as decimals; Databricks SQL `/` also returns a decimal/double. No change needed. Watch `div`.

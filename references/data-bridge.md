# Getting QuickSight's data into Unity Catalog

AI/BI only reads Unity Catalog. Every QuickSight dataset source must resolve to a UC table or
view before you build anything. Record the mapping in `mapping.json` in the output folder:

```json
{
  "retail.bakehouse.sales_transactions": "main.sales.sales_transactions",
  "custom_sql:customer_360": "main.sales.v_customer_360"
}
```

## By data source type (`datasources[].type` in the spec)

| QuickSight source | Bridge | Notes |
|---|---|---|
| REDSHIFT | Lakehouse Federation: `CREATE CONNECTION … TYPE redshift` + `CREATE FOREIGN CATALOG … OPTIONS (database '<db>')` | Fastest path; queries push down to Redshift. Move hot tables to Delta later for performance and cost. |
| ATHENA (Glue tables, Iceberg/Parquet on S3) | Glue catalog federation into UC, or register the S3 Iceberg/Delta tables as UC external tables | No data copy; needs IAM/storage credential setup. |
| RDS / Aurora (MySQL, PostgreSQL), SQL Server, Oracle, Snowflake | Lakehouse Federation connection of that type | |
| S3 file / upload | Land as Delta (`read_files`, Auto Loader) | Files don't federate. |
| SPICE (`import_mode: SPICE`) | Point at the **underlying** source, not SPICE | SPICE is a QuickSight cache. AI/BI queries live, so check the refresh assumptions and size the warehouse. |

Use `databricks-core` for CLI auth/profile. Before creating connections or catalogs, ask the user.
They create shared objects and need secrets, which should come from a secret scope, never inline.

## Verifying the mapping (read-only)
1. `DESCRIBE TABLE <uc_table>`: the table exists.
2. Every column the spec uses exists with a compatible type:
   ```sql
   SELECT column_name, data_type FROM <catalog>.information_schema.columns
   WHERE table_schema = '<schema>' AND table_name = '<table>';
   ```
   Compare against `datasets[].sources[].columns` and every column referenced by visuals and filters.
3. `SELECT count(*) FROM <uc_table>` should be within the expected range of the QuickSight row count. Ask the user when unsure.

## Custom SQL datasets
Rewrite `sources[].sql` against UC names, then run it with `LIMIT 10`. Common Redshift → Databricks SQL changes:

| Redshift | Databricks SQL |
|---|---|
| `GETDATE()`, `SYSDATE` | `current_timestamp()` |
| `TOP n` | `LIMIT n` |
| `LISTAGG(x, ',') WITHIN GROUP (ORDER BY y)` | `array_join(array_sort(collect_list(x)), ',')` (or `string_agg`/`listagg` where supported) |
| `NVL`, `NVL2`, `DECODE`, `DATEADD`, `DATEDIFF`, `DATE_PART`, `ILIKE`, `::type` | supported as-is (`DATEDIFF(unit, a, b)` → prefer `timestampdiff`) |
| `CONVERT_TIMEZONE('A','B',ts)` | `convert_timezone('A','B',ts)` |
| `REGEXP_SUBSTR` | `regexp_extract` |
| `x::varchar` | `CAST(x AS STRING)` |

Keep the rewritten SQL as a UC view (`v_<dataset>`) when several dashboards share it, so the metric view's `source:` points at a stable name.

# Carrying QuickSight security over to Unity Catalog

QuickSight enforces security inside the BI tool. In Databricks it moves into Unity Catalog, on
the tables. The same rules then hold in AI/BI, Genie, the SQL editor and any external BI tool.
Dashboards must be **published without embedded credentials** so each viewer queries as
themselves. With embedded credentials, everyone sees the publisher's data.

## Row-level security (`security.row_level` in the spec)

A QuickSight RLS permission dataset is a table of rules:
- Its columns are `UserName` and/or `GroupName`, plus one or more filter columns (e.g. `area_name`).
- A blank cell means "all values".
- `PermissionPolicy` is `GRANT_ACCESS` (default) or `DENY_ACCESS`.

Pattern:
1. **Entitlements table.** Migrate the rules table to UC as `<schema>.security_entitlements`, one row per user/group. Map QuickSight user names to workspace identities (emails); ask the user how they map. Use `'ALL'` for blank.
2. **Row filter function and attach:**
   ```sql
   CREATE OR REPLACE FUNCTION <schema>.rls_area(area_value STRING)
   RETURNS BOOLEAN
   RETURN EXISTS (
     SELECT 1 FROM <schema>.security_entitlements e
     WHERE (e.user_email = current_user() OR is_account_group_member(e.group_name))
       AND e.area IN ('ALL', area_value));
   ALTER TABLE <table> SET ROW FILTER <schema>.rls_area ON (area_name);
   ```
   For `DENY_ACCESS`, invert: `NOT EXISTS (… AND e.area = area_value)`.
3. Several filter columns: one function with several arguments, `ON (col1, col2)`.
4. Users with no rule see nothing. This matches QuickSight's behaviour for users missing from a GRANT dataset.

**Tag-based RLS** (`security.tag_rls`, for embedded/anonymous sessions) has no direct equivalent. Design per case: account groups, or a mapping table keyed on `current_user()`. External embedding needs its own design review.

## Column-level security (`security.column_level`)
QuickSight CLS hides columns from everyone except the listed principals. Use UC column masks:
```sql
CREATE OR REPLACE FUNCTION <schema>.mask_null(v STRING) RETURNS STRING
RETURN CASE WHEN is_account_group_member('<allowed group>') THEN v END;
ALTER TABLE <table> ALTER COLUMN <col> SET MASK <schema>.mask_null;
```
- To keep last-4 visible: `concat(repeat('*', greatest(length(v)-4,0)), right(v,4))`.
- To keep `count(DISTINCT)` exact on a masked ID: `sha2(v, 256)`.

## Prove it
1. Run as a restricted test user, or switch your own entitlement row to a restricted persona.
2. Check: `min(area) = max(area) = entitled value`.
3. Check: masked columns match the mask pattern (`'^[*]+.{4}$'`, 64-char hex, or NULL).
4. Then open the dashboard as that user.

These steps change shared objects (`ALTER TABLE`, functions, entitlements). Ask before running them in any workspace that isn't a personal scratch one.

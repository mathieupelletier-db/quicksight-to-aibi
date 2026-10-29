"""Build synthetic QuickSight asset bundles (.qs) for the skill's tests and evals.

Both bundles describe reports over the Databricks sample dataset `samples.bakehouse` (present in
every workspace). The QuickSight-side names are:
  Redshift database `retail`, schema `bakehouse`  -> samples.bakehouse.<same table>
  Athena database `crm`                           -> samples.bakehouse.<same table>
Shapes follow the QuickSight API (Definition / DataSet / DataSource / Topic / Theme).

    python3 build_fixtures.py    # writes bakehouse_sales.qs and customer_360.qs next to this file
"""
import json
import os
import zipfile

ACCT, REGION = "111122223333", "us-east-1"
ARN = f"arn:aws:quicksight:{REGION}:{ACCT}"
HERE = os.path.dirname(os.path.abspath(__file__))


def col(ds, name):
    return {"DataSetIdentifier": ds, "ColumnName": name}


def dim(ds, name, fid, kind="Categorical", gran=None):
    body = {"FieldId": fid, "Column": col(ds, name)}
    if gran:
        body["DateGranularity"] = gran
    return {f"{kind}DimensionField": body}


def num(ds, name, fid, agg="SUM"):
    return {"NumericalMeasureField": {"FieldId": fid, "Column": col(ds, name),
                                      "AggregationFunction": {"SimpleNumericalAggregation": agg}}}


def calc_ref(ds, name, fid):
    return {"NumericalMeasureField": {"FieldId": fid, "Column": col(ds, name)}}


def dcount(ds, name, fid):
    return {"CategoricalMeasureField": {"FieldId": fid, "Column": col(ds, name),
                                        "AggregationFunction": "DISTINCT_COUNT"}}


def title(text):
    return {"Visibility": "VISIBLE", "FormatText": {"PlainText": text}}


def grid(*els):
    return [{"Configuration": {"GridLayout": {"Elements": [
        {"ElementId": e, "ElementType": "VISUAL", "ColumnIndex": c, "ColumnSpan": cs,
         "RowIndex": r, "RowSpan": rs} for e, c, cs, r, rs in els]}}}]


def freeform(*els):
    return [{"Configuration": {"FreeFormLayout": {"Elements": [
        {"ElementId": e, "ElementType": "VISUAL", "XAxisLocation": f"{x}px", "YAxisLocation": f"{y}px",
         "Width": f"{w}px", "Height": f"{h}px"} for e, x, y, w, h in els]}}}]


def rel_table(src, schema, table, cols, catalog=None):
    t = {"DataSourceArn": f"{ARN}:datasource/{src}", "Schema": schema, "Name": table,
         "InputColumns": [{"Name": n, "Type": ty} for n, ty in cols]}
    if catalog:
        t["Catalog"] = catalog
    return {"RelationalTable": t}


REDSHIFT = {"DataSourceId": "redshift-retail", "Name": "Retail EDW (Redshift)", "Type": "REDSHIFT",
            "DataSourceParameters": {"RedshiftParameters": {
                "Host": "retail-edw.example.redshift.amazonaws.com", "Port": 5439, "Database": "retail"}}}
ATHENA = {"DataSourceId": "athena-crm", "Name": "CRM lake (Athena)", "Type": "ATHENA",
          "DataSourceParameters": {"AthenaParameters": {"WorkGroup": "primary"}}}

# ------------------------------------------------------------------ Bakehouse Sales Performance

TX_COLS = [("transactionID", "INTEGER"), ("customerID", "INTEGER"), ("franchiseID", "INTEGER"),
           ("dateTime", "DATETIME"), ("product", "STRING"), ("quantity", "INTEGER"),
           ("unitPrice", "INTEGER"), ("totalPrice", "INTEGER"), ("paymentMethod", "STRING"),
           ("cardNumber", "INTEGER")]
FR_COLS = [("franchiseID", "INTEGER"), ("name", "STRING"), ("city", "STRING"), ("district", "STRING"),
           ("country", "STRING"), ("size", "STRING"), ("longitude", "DECIMAL"), ("latitude", "DECIMAL")]
SALES_DS = {
    "DataSetId": "ds-sales", "Name": "Sales with franchise", "ImportMode": "DIRECT_QUERY",
    "PhysicalTableMap": {
        "tx": rel_table("redshift-retail", "bakehouse", "sales_transactions", TX_COLS),
        "fr": rel_table("redshift-retail", "bakehouse", "sales_franchises", FR_COLS)},
    "LogicalTableMap": {
        "ltx": {"Alias": "sales_transactions", "Source": {"PhysicalTableId": "tx"}},
        "lfr": {"Alias": "sales_franchises", "Source": {"PhysicalTableId": "fr"},
                "DataTransforms": [{"RenameColumnOperation": {"ColumnName": "name", "NewColumnName": "franchise_name"}},
                                   {"RenameColumnOperation": {"ColumnName": "franchiseID", "NewColumnName": "fr_franchiseID"}}]},
        "joined": {"Alias": "sales", "Source": {"JoinInstruction": {
            "LeftOperand": "ltx", "RightOperand": "lfr", "Type": "INNER",
            "OnClause": "{franchiseID} = {fr_franchiseID}"}},
            "DataTransforms": [
                {"CreateColumnsOperation": {"Columns": [
                    {"ColumnName": "Price Band", "ColumnId": "c-band",
                     "Expression": "ifelse({unitPrice} >= 5, 'Premium', 'Standard')"},
                    {"ColumnName": "Sale Day", "ColumnId": "c-day", "Expression": "truncDate('DD', {dateTime})"}]}},
                {"FilterOperation": {"ConditionExpression": "{paymentMethod} <> 'test'"}}]},
    },
}
S = "sales"
SALES_DASH = {
    "DashboardId": "dash-bakehouse-sales", "Name": "Bakehouse Sales Performance",
    "Definition": {
        "DataSetIdentifierDeclarations": [{"Identifier": S, "DataSetArn": f"{ARN}:dataset/ds-sales"}],
        "ParameterDeclarations": [{"IntegerParameterDeclaration": {
            "ParameterValueType": "SINGLE_VALUED", "Name": "MinQuantity", "DefaultValues": {"StaticValues": [5]}}}],
        "CalculatedFields": [
            {"DataSetIdentifier": S, "Name": "Avg Ticket", "Expression": "sum({totalPrice}) / distinct_count({transactionID})"},
            {"DataSetIdentifier": S, "Name": "Bulk Revenue", "Expression": "sumIf({totalPrice}, {quantity} >= ${MinQuantity})"},
            {"DataSetIdentifier": S, "Name": "Revenue DoD %",
             "Expression": "periodOverPeriodPercentDifference(sum({totalPrice}), {dateTime}, DAY, 1)"},
            {"DataSetIdentifier": S, "Name": "Card Share",
             "Expression": "sumIf({totalPrice}, {paymentMethod} = 'visa') / sum({totalPrice})"},
        ],
        "FilterGroups": [
            {"FilterGroupId": "fg-product", "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
             "ScopeConfiguration": {"AllSheets": {}},
             "Filters": [{"CategoryFilter": {"FilterId": "f-product", "Column": col(S, "product"),
                                             "Configuration": {"CustomFilterConfiguration": {
                                                 "MatchOperator": "CONTAINS", "CategoryValue": "Tradi",
                                                 "NullOption": "NON_NULLS_ONLY"}}}}]},
            {"FilterGroupId": "fg-qty", "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
             "ScopeConfiguration": {"AllSheets": {}},
             "Filters": [{"NumericRangeFilter": {"FilterId": "f-qty", "Column": col(S, "quantity"),
                                                 "IncludeMinimum": True, "IncludeMaximum": True,
                                                 "RangeMinimum": {"StaticValue": 1}, "RangeMaximum": {"StaticValue": 50},
                                                 "NullOption": "NON_NULLS_ONLY"}}]},
            {"FilterGroupId": "fg-date", "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
             "ScopeConfiguration": {"AllSheets": {}},
             "Filters": [{"TimeRangeFilter": {"FilterId": "f-date", "Column": col(S, "dateTime"),
                                              "TimeGranularity": "DAY", "NullOption": "NON_NULLS_ONLY"}}]},
            {"FilterGroupId": "fg-top", "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
             "ScopeConfiguration": {"SelectedSheets": {"SheetVisualScopingConfigurations": [
                 {"SheetId": "s-overview", "Scope": "SELECTED_VISUALS", "VisualIds": ["v-topfr"]}]}},
             "Filters": [{"TopBottomFilter": {"FilterId": "f-top10", "Column": col(S, "franchise_name"), "Limit": 10,
                                              "AggregationSortConfigurations": [{"Column": col(S, "totalPrice"),
                                                                                 "SortDirection": "DESC",
                                                                                 "AggregationFunction": {"SimpleNumericalAggregation": "SUM"}}]}}]},
        ],
        "Sheets": [
            {"SheetId": "s-overview", "Name": "Overview",
             "FilterControls": [
                 {"DateTimePicker": {"FilterControlId": "fc-date", "Title": "Date", "SourceFilterId": "f-date", "Type": "DATE_RANGE"}},
                 {"TextField": {"FilterControlId": "fc-product", "Title": "Product contains", "SourceFilterId": "f-product"}}],
             "ParameterControls": [{"Slider": {"ParameterControlId": "pc-minq", "Title": "Bulk order min quantity",
                                               "SourceParameterName": "MinQuantity", "MinimumValue": 1, "MaximumValue": 50, "StepSize": 1}}],
             "Visuals": [
                 {"KPIVisual": {"VisualId": "v-rev", "Title": title("Revenue"),
                                "ChartConfiguration": {"FieldWells": {"Values": [num(S, "totalPrice", "rev")]}}}},
                 {"KPIVisual": {"VisualId": "v-ticket", "Title": title("Avg ticket"),
                                "ChartConfiguration": {"FieldWells": {"Values": [calc_ref(S, "Avg Ticket", "tk")]}}}},
                 {"KPIVisual": {"VisualId": "v-bulk", "Title": title("Bulk order revenue"),
                                "ChartConfiguration": {"FieldWells": {"Values": [calc_ref(S, "Bulk Revenue", "bk")]}}}},
                 {"ComboChartVisual": {"VisualId": "v-daily", "Title": title("Daily revenue and avg ticket"),
                                       "ChartConfiguration": {"FieldWells": {"ComboChartAggregatedFieldWells": {
                                           "Category": [dim(S, "dateTime", "d1", "Date", "DAY")],
                                           "BarValues": [num(S, "totalPrice", "rev2")],
                                           "LineValues": [calc_ref(S, "Avg Ticket", "tk2")]}}}}},
                 {"BarChartVisual": {"VisualId": "v-product", "Title": title("Revenue by product and payment"),
                                     "ChartConfiguration": {"FieldWells": {"BarChartAggregatedFieldWells": {
                                         "Category": [dim(S, "product", "p1")], "Values": [num(S, "totalPrice", "rev3")],
                                         "Colors": [dim(S, "paymentMethod", "pm")]}}}}},
                 {"FilledMapVisual": {"VisualId": "v-map", "Title": title("Revenue by country"),
                                      "ChartConfiguration": {"FieldWells": {"FilledMapAggregatedFieldWells": {
                                          "Geospatial": [dim(S, "country", "c1")], "Values": [num(S, "totalPrice", "rev4")]}}}}},
                 {"TableVisual": {"VisualId": "v-topfr", "Title": title("Top 10 franchises"),
                                  "ChartConfiguration": {"FieldWells": {"TableAggregatedFieldWells": {
                                      "GroupBy": [dim(S, "franchise_name", "fn"), dim(S, "city", "ct")],
                                      "Values": [num(S, "totalPrice", "rev5"), dcount(S, "transactionID", "tx")]}}}}},
             ],
             "Layouts": grid(("v-rev", 0, 12, 0, 6), ("v-ticket", 12, 12, 0, 6), ("v-bulk", 24, 12, 0, 6),
                             ("v-daily", 0, 36, 6, 12), ("v-product", 0, 18, 18, 12), ("v-map", 18, 18, 18, 12),
                             ("v-topfr", 0, 36, 30, 12))},
            {"SheetId": "s-products", "Name": "Products",
             "Visuals": [
                 {"LineChartVisual": {"VisualId": "v-trend", "Title": title("Daily revenue by product"),
                                      "ChartConfiguration": {"FieldWells": {"LineChartAggregatedFieldWells": {
                                          "Category": [dim(S, "dateTime", "d2", "Date", "DAY")],
                                          "Values": [num(S, "totalPrice", "rev6")], "Colors": [dim(S, "product", "p2")]}}}}},
                 {"PivotTableVisual": {"VisualId": "v-pivot", "Title": title("Units by country and product"),
                                       "ChartConfiguration": {"FieldWells": {"PivotTableAggregatedFieldWells": {
                                           "Rows": [dim(S, "country", "c2"), dim(S, "city", "ct2")],
                                           "Columns": [dim(S, "product", "p3")],
                                           "Values": [num(S, "quantity", "q1"), calc_ref(S, "Revenue DoD %", "dod")]}}}}},
                 {"TreeMapVisual": {"VisualId": "v-tree", "Title": title("Revenue by franchise size"),
                                    "ChartConfiguration": {"FieldWells": {"TreeMapAggregatedFieldWells": {
                                        "Groups": [dim(S, "size", "sz")], "Sizes": [num(S, "totalPrice", "rev7")]}}}}},
                 {"WaterfallVisual": {"VisualId": "v-water", "Title": title("Revenue bridge by product"),
                                      "ChartConfiguration": {"FieldWells": {"WaterfallChartAggregatedFieldWells": {
                                          "Categories": [dim(S, "product", "p4")], "Values": [num(S, "totalPrice", "rev8")]}}}}},
             ],
             "Layouts": freeform(("v-trend", 0, 0, 1600, 400), ("v-pivot", 0, 400, 800, 500),
                                 ("v-tree", 800, 400, 400, 500), ("v-water", 1200, 400, 400, 500))},
        ],
    },
}
THEME = {"ThemeId": "theme-bakehouse", "Name": "Bakehouse brand", "BaseThemeId": "CLASSIC",
         "Configuration": {"DataColorPalette": {"Colors": ["#8C4A2F", "#D9A441", "#4F7A5A", "#2F5D8C", "#B5651D"],
                                                "EmptyFillColor": "#F2EDE4"},
                           "UIColorPalette": {"PrimaryBackground": "#FFFFFF", "PrimaryForeground": "#2B2B2B",
                                              "Accent": "#8C4A2F"}}}
TOPIC = {"TopicId": "topic-bakehouse-sales", "Topic": {
    "Name": "Bakehouse sales Q&A", "Description": "Ask about bakehouse revenue, products and franchises",
    "DataSets": [{"DatasetArn": f"{ARN}:dataset/ds-sales", "DatasetName": "Sales with franchise",
                  "Columns": [
                      {"ColumnName": "totalPrice", "ColumnFriendlyName": "Revenue", "ColumnSynonyms": ["sales", "turnover"],
                       "ColumnDescription": "Transaction amount in USD", "ColumnDataRole": "MEASURE", "Aggregation": "SUM"},
                      {"ColumnName": "product", "ColumnFriendlyName": "Product", "ColumnSynonyms": ["item", "cookie"],
                       "ColumnDataRole": "DIMENSION"},
                      {"ColumnName": "franchise_name", "ColumnFriendlyName": "Franchise", "ColumnSynonyms": ["store", "shop"],
                       "ColumnDataRole": "DIMENSION"},
                      {"ColumnName": "cardNumber", "ColumnFriendlyName": "Card number", "IsIncludedInTopic": False}],
                  "CalculatedFields": [{"CalculatedFieldName": "Avg Ticket",
                                        "Expression": "sum({totalPrice}) / distinct_count({transactionID})",
                                        "CalculatedFieldSynonyms": ["average basket", "average order value"]}]}]}}

# ------------------------------------------------------------------ Customer 360

C360_SQL = ("SELECT c.customerID, c.first_name, c.last_name, c.email_address, c.phone_number, c.city, c.country, "
            "c.continent, c.gender, count(t.transactionID) AS orders, sum(t.totalPrice) AS lifetime_value, "
            "max(t.dateTime) AS last_order_ts "
            "FROM crm.sales_customers c LEFT JOIN crm.sales_transactions t ON t.customerID = c.customerID "
            "WHERE c.continent IS NOT NULL GROUP BY 1,2,3,4,5,6,7,8,9")
C360_COLS = ([{"Name": n, "Type": "STRING"} for n in ("first_name", "last_name", "email_address", "phone_number",
                                                       "city", "country", "continent", "gender")]
             + [{"Name": "customerID", "Type": "INTEGER"}, {"Name": "orders", "Type": "INTEGER"},
                {"Name": "lifetime_value", "Type": "INTEGER"}, {"Name": "last_order_ts", "Type": "DATETIME"}])
C360_DS = {
    "DataSetId": "ds-c360", "Name": "Customer 360", "ImportMode": "SPICE",
    "PhysicalTableMap": {"p1": {"CustomSql": {"DataSourceArn": f"{ARN}:datasource/athena-crm", "Name": "customer_360",
                                              "SqlQuery": C360_SQL, "Columns": C360_COLS}}},
    "LogicalTableMap": {"l1": {"Alias": "customer_360", "Source": {"PhysicalTableId": "p1"}, "DataTransforms": [
        {"CreateColumnsOperation": {"Columns": [
            {"ColumnName": "Customer Name", "ColumnId": "c-name", "Expression": "concat({first_name}, ' ', {last_name})"},
            {"ColumnName": "Value Tier", "ColumnId": "c-tier",
             "Expression": "ifelse({lifetime_value} >= 100, 'Gold', {lifetime_value} >= 30, 'Silver', 'Bronze')"}]}},
        {"FilterOperation": {"ConditionExpression": "isNotNull({gender})"}}]}},
    "RowLevelPermissionDataSet": {"Namespace": "default", "Arn": f"{ARN}:dataset/ds-c360-rls",
                                  "PermissionPolicy": "GRANT_ACCESS", "FormatVersion": "VERSION_2", "Status": "ENABLED"},
    "ColumnLevelPermissionRules": [{"Principals": [f"{ARN}:group/default/CRM-Admins"],
                                    "ColumnNames": ["email_address", "phone_number"]}],
}
C360_RLS = {
    "DataSetId": "ds-c360-rls", "Name": "Customer 360 RLS rules", "ImportMode": "SPICE",
    "PhysicalTableMap": {"p1": rel_table("athena-crm", "crm", "c360_rls_rules",
                                         [("GroupName", "STRING"), ("country", "STRING")], catalog="AwsDataCatalog")},
    "LogicalTableMap": {"l1": {"Alias": "c360_rls_rules", "Source": {"PhysicalTableId": "p1"}}},
}
REFRESH = {"DataSetId": "ds-c360", "Schedule": {"ScheduleId": "daily-0600", "RefreshType": "FULL_REFRESH",
                                                "ScheduleFrequency": {"Interval": "DAILY", "TimeOfTheDay": "06:00",
                                                                      "Timezone": "America/Toronto"}}}
C = "c360"
C360_DASH = {
    "DashboardId": "dash-customer-360", "Name": "Customer 360",
    "Definition": {
        "DataSetIdentifierDeclarations": [{"Identifier": C, "DataSetArn": f"{ARN}:dataset/ds-c360"}],
        "CalculatedFields": [
            {"DataSetIdentifier": C, "Name": "Days Since Last Order",
             "Expression": "dateDiff({last_order_ts}, now(), 'DD')"},
            {"DataSetIdentifier": C, "Name": "Active Customers",
             "Expression": "distinct_countIf({customerID}, {orders} > 0)"},
            {"DataSetIdentifier": C, "Name": "Country Share",
             "Expression": "percentOfTotal(sum({lifetime_value}), [{country}])"},
            {"DataSetIdentifier": C, "Name": "Email Domain",
             "Expression": "substring({email_address}, locate({email_address}, '@') + 1, 100)"},
        ],
        "FilterGroups": [{"FilterGroupId": "fg-country", "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
                          "ScopeConfiguration": {"AllSheets": {}},
                          "Filters": [{"CategoryFilter": {"FilterId": "f-country", "Column": col(C, "country"),
                                                          "Configuration": {"FilterListConfiguration": {
                                                              "MatchOperator": "EQUALS", "SelectAllOptions": "FILTER_ALL_VALUES"}}}}]}],
        "Sheets": [{
            "SheetId": "s-c360", "Name": "Customers",
            "FilterControls": [{"Dropdown": {"FilterControlId": "fc-country", "Title": "Country",
                                             "SourceFilterId": "f-country", "Type": "MULTI_SELECT"}}],
            "Visuals": [
                {"KPIVisual": {"VisualId": "v-cust", "Title": title("Customers"),
                               "ChartConfiguration": {"FieldWells": {"Values": [dcount(C, "customerID", "cid")]}}}},
                {"KPIVisual": {"VisualId": "v-active", "Title": title("Active customers"),
                               "ChartConfiguration": {"FieldWells": {"Values": [calc_ref(C, "Active Customers", "ac")]}}}},
                {"GaugeChartVisual": {"VisualId": "v-gauge", "Title": title("Avg lifetime value"),
                                      "ChartConfiguration": {"FieldWells": {"Values": [num(C, "lifetime_value", "ltv", "AVERAGE")],
                                                                            "TargetValues": [num(C, "lifetime_value", "ltvmax", "MAX")]}}}},
                {"BarChartVisual": {"VisualId": "v-country", "Title": title("Lifetime value by country and tier"),
                                    "ChartConfiguration": {"FieldWells": {"BarChartAggregatedFieldWells": {
                                        "Category": [dim(C, "country", "co")], "Values": [num(C, "lifetime_value", "ltv2")],
                                        "Colors": [dim(C, "Value Tier", "vt")]}}}}},
                {"WordCloudVisual": {"VisualId": "v-city", "Title": title("Cities"),
                                     "ChartConfiguration": {"FieldWells": {"WordCloudAggregatedFieldWells": {
                                         "GroupBy": [dim(C, "city", "ci")], "Size": [calc_ref(C, "Country Share", "cs")]}}}}},
                {"TableVisual": {"VisualId": "v-detail", "Title": title("Customer detail"),
                                 "ChartConfiguration": {"FieldWells": {"TableUnaggregatedFieldWells": {"Values": [
                                     {"FieldId": "t1", "Column": col(C, "Customer Name")},
                                     {"FieldId": "t2", "Column": col(C, "email_address")},
                                     {"FieldId": "t3", "Column": col(C, "country")},
                                     {"FieldId": "t4", "Column": col(C, "lifetime_value")}]}}},
                                 "Actions": [{"CustomActionId": "a-crm", "Name": "Open in CRM", "Status": "ENABLED",
                                              "Trigger": "DATA_POINT_MENU", "ActionOperations": [{"URLOperation": {
                                                  "URLTemplate": "https://crm.example.com/customer/<<$customerID>>",
                                                  "URLTarget": "NEW_TAB"}}]}]}},
            ],
            "Layouts": grid(("v-cust", 0, 12, 0, 6), ("v-active", 12, 12, 0, 6), ("v-gauge", 24, 12, 0, 6),
                            ("v-country", 0, 24, 6, 12), ("v-city", 24, 12, 6, 12), ("v-detail", 0, 36, 18, 16)),
        }],
    },
}


def write_bundle(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, doc in files.items():
            z.writestr(name, json.dumps(doc, indent=1))


def main():
    write_bundle(os.path.join(HERE, "bakehouse_sales.qs"), {
        "dashboard/dash-bakehouse-sales.json": SALES_DASH, "dataset/ds-sales.json": SALES_DS,
        "datasource/redshift-retail.json": REDSHIFT, "theme/theme-bakehouse.json": THEME,
        "topic/topic-bakehouse-sales.json": TOPIC})
    write_bundle(os.path.join(HERE, "customer_360.qs"), {
        "dashboard/dash-customer-360.json": C360_DASH, "dataset/ds-c360.json": C360_DS,
        "dataset/ds-c360-rls.json": C360_RLS, "datasource/athena-crm.json": ATHENA,
        "refreshschedule/ds-c360-daily.json": REFRESH})
    print("wrote bakehouse_sales.qs, customer_360.qs")


if __name__ == "__main__":
    main()

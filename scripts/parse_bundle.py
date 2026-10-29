"""Parse a QuickSight asset bundle (.qs zip, unzipped folder, or describe-* JSON files) into a
normalized migration spec for AI/BI: datasets + sources, calculated fields translated to
Databricks SQL, visuals mapped to AI/BI widget types with suggested positions, filters,
parameters, security, and a gap list.

    python3 parse_bundle.py bundle.qs -o spec.json [--summary summary.md]
    python3 parse_bundle.py exported_dir/ another.json -o spec.json

Files are classified by content, not by name or folder, so it works with asset-bundle exports
and with `describe-dashboard-definition` / `describe-data-set` / `describe-data-source` output.
"""
import argparse
import json
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qs_expr import translate  # noqa: E402

# QuickSight visual type -> (AI/BI widget type or None, note shown in the gap list)
VISUALS = {
    "BarChartVisual": ("bar", None),
    "LineChartVisual": ("line", None),
    "KPIVisual": ("counter", "KPI trend/target comparison: counter shows one value; add a separate line for the sparkline"),
    "TableVisual": ("table", None),
    "PivotTableVisual": ("pivot", "Check subtotals/expand-collapse; AI/BI pivots don't collapse rows"),
    "PieChartVisual": ("pie", None),
    "ComboChartVisual": ("combo", "Put line series on y.secondary for a dual axis; check series order"),
    "ScatterPlotVisual": ("scatter", None),
    "HeatMapVisual": ("heatmap", None),
    "HistogramVisual": ("histogram", None),
    "BoxPlotVisual": ("box", None),
    "FilledMapVisual": ("choropleth-map", "Check the region type (province/state) is supported by the AI/BI choropleth"),
    "GeospatialMapVisual": ("symbol-map", None),
    "SankeyDiagramVisual": ("sankey", None),
    "FunnelChartVisual": ("funnel", None),
    "GaugeChartVisual": ("custom-vega-viz", "No native gauge: Vega-Lite custom visualization (or a counter)"),
    "WaterfallVisual": ("waterfall", None),
    "TreeMapVisual": (None, "No treemap (not in AI/BI or Vega-Lite): use a sorted bar chart"),
    "WordCloudVisual": (None, "No word cloud widget: use a sorted bar chart or table"),
    "RadarChartVisual": ("custom-vega-viz", "No native radar: Vega-Lite custom visualization or grouped bars"),
    "InsightVisual": ("text", "Narrative/ML insight: rewrite as a text widget, or point users to Genie"),
    "CustomContentVisual": ("text", "Custom content (URL/image/HTML): recreate as a text/markdown widget"),
    "EmptyVisual": (None, "Empty placeholder visual: drop"),
    "LayerMapVisual": (None, "Layer map: no direct equivalent; use symbol-map / choropleth-map"),
    "PluginVisual": (None, "Plugin visual: no direct equivalent"),
}
DIM_KEYS = ("CategoricalDimensionField", "NumericalDimensionField", "DateDimensionField")
MEASURE_KEYS = ("NumericalMeasureField", "CategoricalMeasureField", "DateMeasureField",
                "CalculatedMeasureField")
FILTER_KEYS = ("CategoryFilter", "NumericRangeFilter", "NumericEqualityFilter", "TimeRangeFilter",
               "TimeEqualityFilter", "RelativeDatesFilter", "TopBottomFilter")
QS_GRID_COLUMNS = 36   # QuickSight grid layout width
AIBI_COLUMNS = 12      # AI/BI dashboard grid width (layoutVersion GRID_V1)


# ---------------------------------------------------------------- loading

def _iter_json(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in sorted(files):
                    if f.endswith(".json"):
                        yield os.path.join(root, f), _read(os.path.join(root, f))
        elif zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as z:
                for n in sorted(z.namelist()):
                    if n.endswith(".json"):
                        yield f"{p}!{n}", json.loads(z.read(n).decode("utf-8-sig"))
        else:
            yield p, _read(p)


def _read(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _unwrap(doc):
    for k in ("DataSet", "DataSource", "Dashboard", "Analysis", "Theme"):
        if isinstance(doc, dict) and isinstance(doc.get(k), dict) and len(doc) <= 4:
            return doc[k]
    return doc


def classify(doc):
    if not isinstance(doc, dict):
        return None
    if isinstance(doc.get("Definition"), dict) and "Sheets" in doc["Definition"]:
        return "analysis" if "AnalysisId" in doc and "DashboardId" not in doc else "dashboard"
    if "TopicId" in doc and isinstance(doc.get("Topic"), dict):
        return "topic"
    if "ThemeId" in doc and ("Configuration" in doc or "Version" in doc):
        return "theme"
    if isinstance(doc.get("Schedule"), dict) and "ScheduleFrequency" in doc["Schedule"]:
        return "refresh"
    if "PhysicalTableMap" in doc:
        return "dataset"
    if "DataSourceParameters" in doc or ("DataSourceId" in doc and "Type" in doc):
        return "datasource"
    return None


def _tail(arn):
    return (arn or "").rsplit("/", 1)[-1]


# ---------------------------------------------------------------- datasets / sources

def parse_datasource(d):
    params = d.get("DataSourceParameters") or {}
    kind, conn = (next(iter(params.items())) if params else (None, {}))
    return {"id": d.get("DataSourceId") or _tail(d.get("Arn")), "name": d.get("Name"),
            "type": d.get("Type"), "parameters_type": kind, "connection": conn}


def parse_dataset(d, gaps):
    ds_id = d.get("DataSetId") or _tail(d.get("Arn"))
    name = d.get("Name") or ds_id
    sources = []
    for key, phys in (d.get("PhysicalTableMap") or {}).items():
        if "RelationalTable" in phys:
            t = phys["RelationalTable"]
            sources.append({"key": key, "type": "table", "datasource": _tail(t.get("DataSourceArn")),
                            "catalog": t.get("Catalog"), "schema": t.get("Schema"), "table": t.get("Name"),
                            "columns": [c["Name"] for c in t.get("InputColumns", [])], "uc_table": None})
        elif "CustomSql" in phys:
            t = phys["CustomSql"]
            sources.append({"key": key, "type": "custom_sql", "datasource": _tail(t.get("DataSourceArn")),
                            "name": t.get("Name"), "sql": t.get("SqlQuery"),
                            "columns": [c["Name"] for c in t.get("Columns", [])]})
            gaps.append(_gap("medium", f"dataset {name}", "Custom SQL dataset",
                             "Rewrite the SQL against Unity Catalog names and run it on the warehouse"))
        elif "S3Source" in phys:
            sources.append({"key": key, "type": "s3", "datasource": _tail(phys["S3Source"].get("DataSourceArn"))})
            gaps.append(_gap("high", f"dataset {name}", "S3 file dataset",
                             "Land the file as a Delta table first (read_files / Auto Loader)"))
    joins, calc_cols, row_filters, renames = [], [], [], {}
    for key, logical in (d.get("LogicalTableMap") or {}).items():
        src = logical.get("Source") or {}
        if "JoinInstruction" in src:
            j = src["JoinInstruction"]
            joins.append({"left": j.get("LeftOperand"), "right": j.get("RightOperand"),
                          "type": j.get("Type"), "on": j.get("OnClause")})
        if "DataSetArn" in src:
            gaps.append(_gap("medium", f"dataset {name}", "Dataset built on another dataset",
                             "Migrate the parent dataset first; reference its UC view"))
        for op in logical.get("DataTransforms", []):
            if "RenameColumnOperation" in op:
                renames[op["RenameColumnOperation"]["ColumnName"]] = op["RenameColumnOperation"]["NewColumnName"]
            for c in (op.get("CreateColumnsOperation") or {}).get("Columns", []):
                t = translate(c.get("Expression", ""))
                calc_cols.append({"name": c.get("ColumnName"), "expression": c.get("Expression"),
                                  "sql": t.sql, "needs_review": t.needs_review})
            if "FilterOperation" in op:
                t = translate(op["FilterOperation"].get("ConditionExpression", ""))
                row_filters.append({"expression": op["FilterOperation"].get("ConditionExpression"),
                                    "sql": t.sql, "needs_review": t.needs_review})
    security = {}
    if d.get("RowLevelPermissionDataSet"):
        r = d["RowLevelPermissionDataSet"]
        security["row_level"] = {"permission_dataset": _tail(r.get("Arn")),
                                 "policy": r.get("PermissionPolicy", "GRANT_ACCESS"),
                                 "status": r.get("Status", "ENABLED")}
        gaps.append(_gap("high", f"dataset {name}", "Row-level security (permission dataset)",
                         "Recreate as a Unity Catalog row filter driven by an entitlements table; "
                         "publish dashboards without embedded credentials"))
    if d.get("RowLevelPermissionTagConfiguration"):
        security["tag_rls"] = d["RowLevelPermissionTagConfiguration"]
        gaps.append(_gap("high", f"dataset {name}", "Tag-based RLS (embedding sessions)",
                         "Map session tags to a UC row filter on current_user()/groups; external embedding needs design"))
    if d.get("ColumnLevelPermissionRules"):
        security["column_level"] = [{"columns": r.get("ColumnNames", []), "principals": r.get("Principals", [])}
                                    for r in d["ColumnLevelPermissionRules"]]
        gaps.append(_gap("medium", f"dataset {name}", "Column-level security",
                         "Recreate as Unity Catalog column masks (mask, hash or NULL)"))
    return {"id": ds_id, "name": name, "import_mode": d.get("ImportMode"), "sources": sources,
            "joins": joins, "renames": renames, "calculated_columns": calc_cols, "row_filters": row_filters,
            "security": security}


# ---------------------------------------------------------------- dashboards

def _gap(severity, where, what, suggestion):
    return {"severity": severity, "where": where, "what": what, "suggestion": suggestion}


def _walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _walk_ctx(obj, well=None):
    """Like _walk, but also yields the name of the field well (the nearest key holding a list)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v, well
            yield from _walk_ctx(v, k if isinstance(v, list) else well)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_ctx(v, well)


def _static(v):
    return (v or {}).get("StaticValue") if isinstance(v, dict) else None


def _title(vis):
    t = (vis.get("Title") or {}).get("FormatText") or {}
    return t.get("PlainText") or t.get("RichText")


def _position(el):
    if el is None:
        return None
    if "ColumnIndex" in el:  # grid layout, 36 columns
        x = round(el.get("ColumnIndex", 0) * AIBI_COLUMNS / QS_GRID_COLUMNS)
        w = max(1, round(el.get("ColumnSpan", 12) * AIBI_COLUMNS / QS_GRID_COLUMNS))
        return {"x": min(x, AIBI_COLUMNS - 1), "y": round(el.get("RowIndex", 0) / 2),
                "width": min(w, AIBI_COLUMNS - min(x, AIBI_COLUMNS - 1)),
                "height": max(2, round(el.get("RowSpan", 12) / 2)), "approximate": True}
    if "XAxisLocation" in el:  # free-form layout in px; assume a 1600px canvas
        px = lambda s: float(str(s).rstrip("px") or 0)  # noqa: E731
        x = round(px(el["XAxisLocation"]) / 1600 * AIBI_COLUMNS)
        return {"x": min(x, AIBI_COLUMNS - 1), "y": round(px(el.get("YAxisLocation", 0)) / 50),
                "width": max(1, round(px(el.get("Width", "400px")) / 1600 * AIBI_COLUMNS)),
                "height": max(2, round(px(el.get("Height", "300px")) / 50)), "approximate": True}
    return None


def parse_dashboard(doc, kind, datasets_by_id, gaps):
    d = doc["Definition"]
    name = doc.get("Name") or doc.get("DashboardId") or doc.get("AnalysisId")
    decl = {x["Identifier"]: _tail(x.get("DataSetArn")) for x in d.get("DataSetIdentifierDeclarations", [])}

    params = []
    for p in d.get("ParameterDeclarations", []):
        (ptype, body), = p.items()
        params.append({"name": body.get("Name"), "type": ptype.replace("ParameterDeclaration", ""),
                       "defaults": (body.get("DefaultValues") or {}).get("StaticValues")})

    calc_raw = {c["Name"]: c for c in d.get("CalculatedFields", [])}
    calc_sql: dict[str, str] = {}

    def resolve(field_name, _seen=()):
        if field_name in calc_raw and field_name not in _seen:
            if field_name not in calc_sql:
                calc_sql[field_name] = translate(calc_raw[field_name]["Expression"],
                                                 resolve=lambda n: resolve(n, _seen + (field_name,))).sql
            return calc_sql[field_name]
        return None

    calcs = []
    for cname, c in calc_raw.items():
        t = translate(c["Expression"], resolve=lambda n, me=cname: resolve(n, (me,)))
        calcs.append({"name": cname, "dataset": decl.get(c.get("DataSetIdentifier"), c.get("DataSetIdentifier")),
                      "expression": c["Expression"], "sql": t.sql, "is_aggregate": t.is_aggregate,
                      "is_table_calc": t.is_table_calc, "parameters": t.parameters,
                      "needs_review": t.needs_review})
        for r in t.needs_review:
            gaps.append(_gap("high" if t.is_table_calc else "medium", f"{name} / calc {cname}", r,
                             "Translate by hand; see references/expression-mapping.md"))
        if t.parameters:
            gaps.append(_gap("medium", f"{name} / calc {cname}",
                             f"Uses parameter(s) {', '.join(t.parameters)} inside the calculation",
                             "Metric views can't take parameters: keep a dashboard dataset with a "
                             "named parameter for this measure"))
    calc_by_name = {c["name"]: c for c in calcs}

    filters = []
    for g in d.get("FilterGroups", []):
        scope = g.get("ScopeConfiguration") or {}
        for f in g.get("Filters", []):
            for fk in FILTER_KEYS:
                if fk in f:
                    body = f[fk]
                    col = body.get("Column") or {}
                    filters.append({"id": body.get("FilterId"), "type": fk, "column": col.get("ColumnName"),
                                    "dataset": decl.get(col.get("DataSetIdentifier")),
                                    "scope": "all_sheets" if "AllSheets" in scope else "selected",
                                    "cross_dataset": g.get("CrossDataset"), "config": body.get("Configuration")})
                    where = f"{name} / filter {body.get('FilterId')} on {col.get('ColumnName')}"
                    cfg = body.get("Configuration") or {}
                    match = next((c.get("MatchOperator") for c in cfg.values() if isinstance(c, dict)
                                  and c.get("MatchOperator")), None)
                    if match and match not in ("EQUALS",):
                        gaps.append(_gap("medium", where, f"Category filter uses {match} match, not exact values",
                                         "AI/BI list filters match exact values: use a LIKE/contains condition in "
                                         "the dataset (with a parameter) or confirm exact match is acceptable"))
                    lo = _static(body.get("RangeMinimum")) or _static(body.get("RangeMinimumValue"))
                    hi = _static(body.get("RangeMaximum")) or _static(body.get("RangeMaximumValue"))
                    if lo is not None or hi is not None:
                        gaps.append(_gap("low", where, f"Filter has a hard-coded range {lo}–{hi}",
                                         "Static bounds silently drop newer/larger values; make them a default "
                                         "selection instead of a fixed filter"))
                    if fk == "TopBottomFilter":
                        gaps.append(_gap("medium", f"{name} / filter {body.get('FilterId')}", "Top/bottom N filter",
                                         "Use ORDER BY ... LIMIT in the dataset or a ranked CTE; dynamic Top-N isn't a widget setting"))

    sheets = []
    for s in d.get("Sheets", []):
        layout = {}
        for lay in s.get("Layouts", []):
            cfg = lay.get("Configuration") or {}
            for key in ("GridLayout", "FreeFormLayout"):
                for el in (cfg.get(key) or {}).get("Elements", []):
                    layout[el.get("ElementId")] = el
            if "SectionBasedLayout" in cfg:
                gaps.append(_gap("low", f"{name} / sheet {s.get('Name')}", "Section-based (paginated) layout",
                                 "Lay out manually; consider PDF subscriptions for print output"))
        visuals = []
        for v in s.get("Visuals", []):
            (vtype, body), = v.items()
            widget, note = VISUALS.get(vtype, (None, f"Unknown visual type {vtype}"))
            dims, measures = [], []
            wells = (body.get("ChartConfiguration") or {}).get("FieldWells") or {}
            for k, val, well in _walk_ctx(wells):
                if k == "TableUnaggregatedFieldWells":
                    for u in val.get("Values", []):
                        col = u.get("Column") or {}
                        dims.append({"field_id": u.get("FieldId"), "column": col.get("ColumnName"),
                                     "dataset": decl.get(col.get("DataSetIdentifier")), "date_granularity": None,
                                     "is_calculated": col.get("ColumnName") in calc_by_name, "unaggregated": True})
                elif k in DIM_KEYS:
                    col = val.get("Column") or {}
                    dims.append({"field_id": val.get("FieldId"), "column": col.get("ColumnName"), "well": well,
                                 "dataset": decl.get(col.get("DataSetIdentifier")),
                                 "date_granularity": val.get("DateGranularity"),
                                 "is_calculated": col.get("ColumnName") in calc_by_name})
                elif k in MEASURE_KEYS:
                    col = val.get("Column") or {}
                    agg = val.get("AggregationFunction")
                    if isinstance(agg, dict):
                        agg = agg.get("SimpleNumericalAggregation") or next(iter(agg.values()), None)
                    cname = col.get("ColumnName")
                    m = {"field_id": val.get("FieldId"), "column": cname, "well": well,
                         "role": "target" if well and "Target" in well else "value",
                         "dataset": decl.get(col.get("DataSetIdentifier")), "aggregation": agg}
                    if k == "CalculatedMeasureField":
                        t = translate(val.get("Expression", ""), resolve=resolve)
                        m.update(expression=val.get("Expression"), sql=t.sql, needs_review=t.needs_review)
                    elif cname in calc_by_name:
                        m.update(calculated_field=cname, sql=calc_by_name[cname]["sql"])
                    else:
                        fn = {"DISTINCT_COUNT": "count(DISTINCT {})", "COUNT": "count({})", "SUM": "sum({})",
                              "AVERAGE": "avg({})", "MIN": "min({})", "MAX": "max({})",
                              "MEDIAN": "median({})"}.get(str(agg).upper(), "sum({})")
                        m["sql"] = fn.format(f"`{cname}`" if cname and not cname.isidentifier() else cname)
                    measures.append(m)
            vis = {"id": body.get("VisualId"), "qs_type": vtype, "aibi_widget": widget,
                   "title": _title(body), "datasets": sorted({x["dataset"] for x in dims + measures if x["dataset"]}),
                   "dimensions": dims, "measures": measures,
                   "position": _position(layout.get(body.get("VisualId"))), "notes": [note] if note else []}
            if note and vtype != "KPIVisual":
                gaps.append(_gap("high" if widget is None else "medium" if widget == "custom-vega-viz" else "low",
                                 f"{name} / {s.get('Name')} / {vis['title'] or vis['id']}", note,
                                 "See references/visual-mapping.md"))
            if body.get("Actions"):
                vis["actions"] = [a.get("Name") for a in body["Actions"]]
                vis["url_actions"] = [op["URLOperation"].get("URLTemplate") for a in body["Actions"]
                                      for op in a.get("ActionOperations", []) if "URLOperation" in op]
                gaps.append(_gap("low", f"{name} / {vis['title'] or vis['id']}", "Visual actions (drill/URL/filter)",
                                 "AI/BI cross-filters on click; recreate URL actions as links in a table column"))
            if body.get("ConditionalFormatting"):
                vis["notes"].append("has conditional formatting")
            visuals.append(vis)
        controls = []
        for c in s.get("FilterControls", []):
            (ctype, body), = c.items()
            controls.append({"kind": "filter", "control": ctype, "title": body.get("Title"),
                             "source": body.get("SourceFilterId")})
        for c in s.get("ParameterControls", []):
            (ctype, body), = c.items()
            controls.append({"kind": "parameter", "control": ctype, "title": body.get("Title"),
                             "source": body.get("SourceParameterName")})
        sheets.append({"id": s.get("SheetId"), "name": s.get("Name"), "visuals": visuals, "controls": controls})

    return {"id": doc.get("DashboardId") or doc.get("AnalysisId"), "name": name, "kind": kind,
            "datasets": [{"identifier": k, "id": v, "name": (datasets_by_id.get(v) or {}).get("name")}
                         for k, v in decl.items()],
            "parameters": params, "calculated_fields": calcs, "filters": filters, "sheets": sheets}


# ---------------------------------------------------------------- spec

def complexity(spec):
    """high: security to rebuild, files to land, missing dependencies, >3 datasets, or several
    hard items (table calcs / visuals without an equivalent); medium: any other notable gap or
    >10 visuals; low otherwise."""
    gaps = spec["gaps"]
    n_vis = sum(len(s["visuals"]) for d in spec["dashboards"] for s in d["sheets"])
    hard = [g for g in gaps if g["severity"] == "high"]
    blocking = [g for g in hard if any(w in g["what"] for w in ("security", "RLS", "S3 file", "not in the bundle",
                                                                 "No dashboard"))]
    if blocking or len(hard) >= 3 or len(spec["datasets"]) > 3:
        return "high"
    if hard or any(g["severity"] == "medium" for g in gaps) or n_vis > 10:
        return "medium"
    return "low"


def parse_topic(doc):
    t = doc["Topic"]
    out = {"id": doc.get("TopicId"), "name": t.get("Name"), "description": t.get("Description"), "datasets": [],
           "columns": [], "calculated_fields": []}
    for ds in t.get("DataSets", []):
        out["datasets"].append(_tail(ds.get("DatasetArn")))
        for c in ds.get("Columns", []):
            out["columns"].append({"dataset": _tail(ds.get("DatasetArn")), "column": c.get("ColumnName"),
                                   "friendly_name": c.get("ColumnFriendlyName"), "description": c.get("ColumnDescription"),
                                   "synonyms": c.get("ColumnSynonyms", []), "role": c.get("ColumnDataRole"),
                                   "aggregation": c.get("Aggregation"), "included": c.get("IsIncludedInTopic", True)})
        for c in ds.get("CalculatedFields", []):
            out["calculated_fields"].append({"name": c.get("CalculatedFieldName"), "expression": c.get("Expression"),
                                             "sql": translate(c.get("Expression", "")).sql,
                                             "synonyms": c.get("CalculatedFieldSynonyms", [])})
    return out


def parse_theme(doc):
    cfg = doc.get("Configuration") or (doc.get("Version") or {}).get("Configuration") or {}
    return {"id": doc.get("ThemeId"), "name": doc.get("Name"),
            "data_colors": (cfg.get("DataColorPalette") or {}).get("Colors", []),
            "ui_colors": cfg.get("UIColorPalette") or {}}


WHERE_RE = re.compile(r"\bWHERE\b(.*?)(?=\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|\bLIMIT\b|\bUNION\b|$)",
                      re.I | re.S)


def row_drops(datasets):
    out = []
    for ds in datasets:
        for f in ds["row_filters"]:
            out.append({"dataset": ds["name"], "kind": "dataset filter", "detail": f["expression"], "sql": f["sql"]})
        for s in ds["sources"]:
            if s["type"] == "custom_sql":
                for m in WHERE_RE.finditer(s.get("sql") or ""):
                    cond = " ".join(m.group(1).split())
                    out.append({"dataset": ds["name"], "kind": "custom SQL WHERE", "detail": cond, "sql": cond})
    return out


FIELD_REF = re.compile(r"\{([^}]+)\}")


def unused_fields(dashboards, datasets):
    defined = {c["name"] for db in dashboards for c in db["calculated_fields"]}
    defined |= {c["name"] for ds in datasets for c in ds["calculated_columns"]}
    used = set()
    for db in dashboards:
        for s in db["sheets"]:
            for v in s["visuals"]:
                used |= {x["column"] for x in v["dimensions"] + v["measures"] if x.get("column")}
                for m in v["measures"]:
                    used |= set(FIELD_REF.findall(m.get("expression") or ""))
        used |= {f["column"] for f in db["filters"] if f.get("column")}
        for c in db["calculated_fields"]:
            used |= set(FIELD_REF.findall(c["expression"])) - {c["name"]}
    for ds in datasets:
        for c in ds["calculated_columns"]:
            used |= set(FIELD_REF.findall(c["expression"] or "")) - {c["name"]}
    return sorted(defined - used)


def build_spec(paths):
    raw = {"dashboard": [], "analysis": [], "dataset": [], "datasource": [], "topic": [], "theme": [], "refresh": []}
    skipped = []
    for where, doc in _iter_json(paths):
        doc = _unwrap(doc)
        kind = classify(doc)
        (raw[kind] if kind else skipped).append(doc if kind else where)
    gaps: list[dict] = []
    datasources = [parse_datasource(d) for d in raw["datasource"]]
    datasets = [parse_dataset(d, gaps) for d in raw["dataset"]]
    by_id = {d["id"]: d for d in datasets}
    raw_by_id = {(d.get("DataSetId") or _tail(d.get("Arn"))): d for d in raw["dataset"]}
    for ds in datasets:
        rl = ds["security"].get("row_level")
        if rl and rl["permission_dataset"] in raw_by_id:
            cols = [c for s in by_id[rl["permission_dataset"]]["sources"] for c in s.get("columns", [])]
            rl["principal_columns"] = [c for c in cols if c in ("UserName", "GroupName", "UserARN", "GroupARN")]
            rl["rule_columns"] = [c for c in cols if c not in rl["principal_columns"]]
            by_id[rl["permission_dataset"]]["role"] = "rls_rules"
    src_type = {s["id"]: s["type"] for s in datasources}
    for ds in datasets:
        for s in ds["sources"]:
            s["datasource_type"] = src_type.get(s.get("datasource"))
        if ds["import_mode"] == "SPICE":
            ds.setdefault("notes", []).append("SPICE: QuickSight serves a cached copy; AI/BI queries the "
                                              "source live — check refresh expectations and warehouse sizing")
    dashboards = [parse_dashboard(d, "dashboard", by_id, gaps) for d in raw["dashboard"]]
    dashboards += [parse_dashboard(d, "analysis", by_id, gaps) for d in raw["analysis"]]
    for db in dashboards:
        for ref in db["datasets"]:
            if ref["id"] not in by_id:
                gaps.append(_gap("high", f"{db['name']}", f"Dataset {ref['id']} referenced but not in the bundle",
                                 "Re-export with --include-all-dependencies, or add describe-data-set output"))
    if not dashboards:
        gaps.append(_gap("high", "bundle", "No dashboard or analysis definition found",
                         "Export with QUICKSIGHT_JSON format and include the dashboard ARN"))
    topics = [parse_topic(t) for t in raw["topic"]]
    if topics:
        gaps.append(_gap("low", "bundle", f"{len(topics)} QuickSight Q topic(s)",
                         "Recreate as a Genie space over the new metric views: friendly names/synonyms -> "
                         "column comments and instructions, calculated fields -> measures"))
    refresh = [{"dataset": r.get("DataSetId"), "id": r["Schedule"].get("ScheduleId"),
                "type": r["Schedule"].get("RefreshType"), "frequency": r["Schedule"].get("ScheduleFrequency")}
               for r in raw["refresh"]]
    spec = {"dashboards": dashboards, "datasets": datasets, "datasources": datasources,
            "topics": topics, "themes": [parse_theme(t) for t in raw["theme"]], "refresh_schedules": refresh,
            "row_drops": row_drops(datasets), "unused_fields": unused_fields(dashboards, datasets),
            "gaps": gaps, "skipped_files": skipped}
    spec["complexity"] = complexity(spec)
    return spec


def summary_md(spec):
    lines = [f"# QuickSight bundle summary — complexity: **{spec['complexity']}**", ""]
    for db in spec["dashboards"]:
        lines.append(f"## {db['kind'].title()}: {db['name']}")
        lines.append(f"- Datasets: {', '.join(d['name'] or d['id'] for d in db['datasets']) or '—'}")
        lines.append(f"- Parameters: {', '.join(p['name'] for p in db['parameters']) or '—'}")
        lines.append(f"- Calculated fields: {len(db['calculated_fields'])}; filters: {len(db['filters'])}")
        for s in db["sheets"]:
            lines.append(f"### Sheet: {s['name']} ({len(s['visuals'])} visuals)")
            lines.append("| Visual | QuickSight | AI/BI | Dimensions | Measures |")
            lines.append("|---|---|---|---|---|")
            for v in s["visuals"]:
                dims = ", ".join(x["column"] or "?" for x in v["dimensions"])
                ms = ", ".join(x.get("calculated_field") or x["column"] or "calc" for x in v["measures"])
                lines.append(f"| {v['title'] or v['id']} | {v['qs_type']} | {v['aibi_widget'] or '**none**'} | {dims} | {ms} |")
        lines.append("")
    lines.append("## Datasets")
    for ds in spec["datasets"]:
        src = "; ".join(f"{s['type']}: {s.get('schema') or ''}.{s.get('table') or s.get('name') or ''} "
                        f"({s.get('datasource_type') or '?'})" for s in ds["sources"])
        lines.append(f"- **{ds['name']}** [{ds['import_mode']}] {src}"
                     + (f" — security: {', '.join(ds['security'])}" if ds["security"] else ""))
    if spec["row_drops"]:
        lines += ["", "## Rows silently dropped before the dashboard (reproduce these)"]
        lines += [f"- {d['dataset']}: {d['kind']} — `{d['detail']}`" for d in spec["row_drops"]]
    if spec["unused_fields"]:
        lines += ["", "## Defined but unused (candidates to drop)", "- " + ", ".join(spec["unused_fields"])]
    for t in spec["topics"]:
        lines += ["", f"## Q topic: {t['name']} → Genie space",
                  f"- {len(t['columns'])} columns ({sum(1 for c in t['columns'] if c['synonyms'])} with synonyms), "
                  f"{len(t['calculated_fields'])} calculated fields"]
    for th in spec["themes"]:
        lines += ["", f"## Theme: {th['name']} — data colors {', '.join(th['data_colors'])}"]
    for r in spec["refresh_schedules"]:
        lines += ["", f"## SPICE refresh: {r['dataset']} {r['type']} {json.dumps(r['frequency'])}"]
    lines += ["", "## Gaps", "| Severity | Where | What | Suggestion |", "|---|---|---|---|"]
    order = {"high": 0, "medium": 1, "low": 2}
    for g in sorted(spec["gaps"], key=lambda g: order[g["severity"]]):
        lines.append(f"| {g['severity']} | {g['where']} | {g['what']} | {g['suggestion']} |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("inputs", nargs="+", help=".qs bundle, folder, or JSON files")
    ap.add_argument("-o", "--out", default="spec.json")
    ap.add_argument("--summary", help="also write a markdown summary here")
    a = ap.parse_args()
    spec = build_spec(a.inputs)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=1)
    md = summary_md(spec)
    if a.summary:
        with open(a.summary, "w", encoding="utf-8") as f:
            f.write(md)
    print(md)


if __name__ == "__main__":
    main()

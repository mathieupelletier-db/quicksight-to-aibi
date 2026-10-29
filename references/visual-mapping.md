# QuickSight → AI/BI visual and control mapping

`parse_bundle.py` already assigns `aibi_widget` to every visual. This file explains the choices,
and what to do where there is no 1:1 widget. For the widget JSON itself (encodings, `frame`,
`version` per widget type), follow the `databricks-aibi-dashboards` skill references. Those are
the source of truth for the `.lvdash.json` format.

## Visuals

| QuickSight | AI/BI `widgetType` | Notes |
|---|---|---|
| KPIVisual | `counter` | A counter shows one value. QuickSight KPI comparisons (vs previous period / target) need a second counter, or a small `line`. Format currency/percent in the counter encoding. |
| BarChartVisual | `bar` | `Colors` field well → color encoding (stacked/grouped). Horizontal bars: swap x/y. |
| LineChartVisual | `line` | `Colors` → series. Date dims: apply `DateGranularity` with `date_trunc` in the dataset or the x-axis time unit. |
| AreaChart (LineChart with area style) | `area` | |
| ComboChartVisual | `combo` | `BarValues` → `y`, `LineValues` → `y.secondary` for a dual axis. |
| PieChartVisual (incl. donut) | `pie` | |
| TableVisual | `table` | Unaggregated wells → raw columns (`disaggregated: true`). Aggregated wells → GROUP BY dataset. Conditional formatting → table column styles where supported. |
| PivotTableVisual | `pivot` | Rows/Columns/Values map directly. AI/BI pivots don't expand/collapse: offer a pivot plus a detail table, or one table per level. |
| ScatterPlotVisual | `scatter` | |
| HeatMapVisual | `heatmap` | |
| HistogramVisual | `histogram` | |
| BoxPlotVisual | `box` | |
| FilledMapVisual | `choropleth-map` | Check the region field is a supported region type (country / state / province). |
| GeospatialMapVisual | `symbol-map` | Needs latitude/longitude columns. |
| SankeyDiagramVisual | `sankey` | |
| FunnelChartVisual | `funnel` | |
| WaterfallVisual | `waterfall` | |
| GaugeChartVisual | `custom-vega-viz` | Vega-Lite arc gauge (see `6-custom-visualizations.md` in the aibi-dashboards skill). A counter is the fallback. |
| RadarChartVisual | `custom-vega-viz` or `bar` | Grouped bars usually read better. |
| TreeMapVisual | `bar` (sorted) | No treemap in AI/BI or Vega-Lite. |
| WordCloudVisual | `bar` (sorted) or `table` | |
| InsightVisual (narratives, forecasts, anomalies) | `text`, or `forecast-line` for forecasts | Point users to Genie for "why" questions. |
| CustomContentVisual | `text` (markdown) | Links/images only; no embedded HTML apps. |
| EmptyVisual | drop | |

## Layout
- QuickSight grid layouts are 36 columns; AI/BI pages use a 12-column grid (`"layoutVersion": "GRID_V1"` on every page). The parser converts `ColumnIndex/ColumnSpan` × 12/36 and `RowIndex/RowSpan` ÷ 2, and marks the result `approximate`.
- After conversion, re-pack rows so each row fills width 12 with no gaps or overlaps. AI/BI rejects overlapping widgets.
- Free-form layouts (px) assume a 1600 px canvas.
- One AI/BI page per QuickSight sheet.

## Filter and parameter controls

| QuickSight control | AI/BI widget |
|---|---|
| Dropdown / List (multi) | `filter-multi-select` |
| Dropdown / List (single) | `filter-single-select` |
| DateTimePicker (range) / RelativeDateTime | `filter-date-range-picker` |
| Slider (range) | `range-slider` |
| TextField / TextArea | `filter-single-select` with search, or a text parameter |
| Parameter control | a dataset parameter (`:name`) bound to a filter widget |

- Filter scope `all_sheets` → global filter (filters page / every page).
- `selected` sheets → page-level filter widgets.
- Cross-dataset filters (`ALL_DATASETS`) need the same field in every bound dataset.

## Interactions
- Visual actions "filter same sheet" → AI/BI cross-filtering on click (on by default).
- URL actions → a table column rendered as a link.
- Navigation actions → page links in a text widget.

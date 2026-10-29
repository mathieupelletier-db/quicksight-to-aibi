import json
import os
import subprocess
import sys
import zipfile

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from parse_bundle import build_spec  # noqa: E402

FIX = os.path.join(HERE, "..", "..", "evals", "fixtures")
SALES = os.path.join(FIX, "bakehouse_sales.qs")
C360 = os.path.join(FIX, "customer_360.qs")


@pytest.fixture(scope="module", autouse=True)
def fixtures():
    subprocess.run([sys.executable, os.path.join(FIX, "build_fixtures.py")], check=True, capture_output=True)


@pytest.fixture(scope="module")
def sales(fixtures):
    return build_spec([SALES])


@pytest.fixture(scope="module")
def c360(fixtures):
    return build_spec([C360])


def visuals(spec):
    return {v["id"]: v for d in spec["dashboards"] for s in d["sheets"] for v in s["visuals"]}


def gap_text(spec):
    return " | ".join(f"{g['where']} :: {g['what']}" for g in spec["gaps"])


# --- structure -------------------------------------------------------------

def test_sales_join_and_renames(sales):
    [ds] = sales["datasets"]
    assert {s["table"] for s in ds["sources"]} == {"sales_transactions", "sales_franchises"}
    assert ds["joins"] == [{"left": "ltx", "right": "lfr", "type": "INNER", "on": "{franchiseID} = {fr_franchiseID}"}]
    assert ds["renames"] == {"name": "franchise_name", "franchiseID": "fr_franchiseID"}


def test_sales_visual_mapping_and_layouts(sales):
    v = visuals(sales)
    assert v["v-daily"]["aibi_widget"] == "combo"
    assert v["v-map"]["aibi_widget"] == "choropleth-map"
    assert v["v-water"]["aibi_widget"] == "waterfall"
    assert v["v-tree"]["aibi_widget"] is None
    # AI/BI GRID_V1 is 12 columns; QuickSight grid is 36
    assert v["v-rev"]["position"] == {"x": 0, "y": 0, "width": 4, "height": 3, "approximate": True}
    assert v["v-ticket"]["position"]["x"] == 4 and v["v-bulk"]["position"]["x"] == 8
    assert v["v-daily"]["position"]["width"] == 12
    assert v["v-trend"]["position"]["width"] == 12 and v["v-pivot"]["position"]["width"] == 6  # free-form px


# --- iteration-2 behaviour --------------------------------------------------

def test_filter_details_surface_contains_match_and_hardcoded_range(sales):
    g = gap_text(sales)
    assert "CONTAINS" in g
    assert "hard-coded range 1–50" in g
    assert "Top/bottom N" in g


def test_row_drops_are_listed(sales, c360):
    assert sales["row_drops"] == [{"dataset": "Sales with franchise", "kind": "dataset filter",
                                   "detail": "{paymentMethod} <> 'test'", "sql": "paymentMethod <> 'test'"}]
    kinds = {(d["kind"], d["detail"]) for d in c360["row_drops"]}
    assert ("dataset filter", "isNotNull({gender})") in kinds
    assert any(k == "custom SQL WHERE" and "continent IS NOT NULL" in d for k, d in kinds)


def test_unused_fields_are_listed(sales, c360):
    assert sorted(sales["unused_fields"]) == ["Card Share", "Price Band", "Sale Day"]
    assert sorted(c360["unused_fields"]) == ["Days Since Last Order", "Email Domain"]


def test_complexity_is_proportionate(sales, c360):
    assert sales["complexity"] == "medium"   # one table calc + one no-equivalent visual
    assert c360["complexity"] == "high"      # RLS + CLS


def test_topic_theme_and_refresh(sales, c360):
    [t] = sales["topics"]
    assert t["name"] == "Bakehouse sales Q&A"
    rev = next(c for c in t["columns"] if c["column"] == "totalPrice")
    assert rev["friendly_name"] == "Revenue" and rev["synonyms"] == ["sales", "turnover"]
    assert next(c for c in t["columns"] if c["column"] == "cardNumber")["included"] is False
    assert t["calculated_fields"][0]["synonyms"] == ["average basket", "average order value"]
    assert sales["themes"][0]["data_colors"][0] == "#8C4A2F"
    assert c360["refresh_schedules"][0]["dataset"] == "ds-c360"
    assert c360["refresh_schedules"][0]["frequency"]["Interval"] == "DAILY"


def test_rls_rules_dataset_is_described(c360):
    c = next(d for d in c360["datasets"] if d["id"] == "ds-c360")
    rl = c["security"]["row_level"]
    assert rl["permission_dataset"] == "ds-c360-rls"
    assert rl["principal_columns"] == ["GroupName"] and rl["rule_columns"] == ["country"]
    assert c["security"]["column_level"][0]["columns"] == ["email_address", "phone_number"]


def test_url_action_and_gauge_target(c360):
    v = visuals(c360)
    assert v["v-gauge"]["aibi_widget"] == "custom-vega-viz"
    assert [m["role"] for m in v["v-gauge"]["measures"]] == ["value", "target"]
    assert v["v-detail"]["url_actions"] == ["https://crm.example.com/customer/<<$customerID>>"]


# --- robustness -------------------------------------------------------------

def test_missing_dataset_dependency_is_a_gap(tmp_path):
    with zipfile.ZipFile(SALES) as z:
        z.extract("dashboard/dash-bakehouse-sales.json", tmp_path)
    spec = build_spec([str(tmp_path)])
    assert any("not in the bundle" in g["what"] for g in spec["gaps"])


def test_describe_api_wrappers_are_unwrapped(tmp_path):
    with zipfile.ZipFile(SALES) as z:
        ds = json.loads(z.read("dataset/ds-sales.json"))
        dash = json.loads(z.read("dashboard/dash-bakehouse-sales.json"))
    (tmp_path / "a.json").write_text(json.dumps({"Status": 200, "DataSet": ds, "RequestId": "x"}))
    (tmp_path / "b.json").write_text(json.dumps({"Status": 200, **dash, "RequestId": "y"}))
    spec = build_spec([str(tmp_path)])
    assert spec["datasets"][0]["id"] == "ds-sales" and len(spec["dashboards"]) == 1

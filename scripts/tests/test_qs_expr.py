import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from qs_expr import translate  # noqa: E402


@pytest.mark.parametrize("qs,sql", [
    ("sum({apps}) / sum({mail_qty})", "sum(apps) / sum(mail_qty)"),
    ("distinct_count({customer_number})", "count(DISTINCT customer_number)"),
    ("ifelse({amt} >= 20000, 'High', {amt} >= 15000, 'Medium', 'Low')",
     "CASE WHEN amt >= 20000 THEN 'High' WHEN amt >= 15000 THEN 'Medium' ELSE 'Low' END"),
    ("dateDiff({created}, now(), 'DD')", "timestampdiff(DAY, created, current_timestamp())"),
    ("dateDiff({a}, {b})", "timestampdiff(DAY, a, b)"),
    ("truncDate('MM', {send_date})", "date_trunc('MONTH', send_date)"),
    ("addDateTime(7, 'DD', {d})", "timestampadd(DAY, 7, d)"),
    ("extract('YYYY', {d})", "extract(YEAR FROM d)"),
    ("right({customer_number}, 4)", "right(customer_number, 4)"),
    ("locate({s}, 'x')", "locate('x', s)"),
    ("toString({year})", "CAST(year AS STRING)"),
    ("isNull({x})", "(x IS NULL)"),
    ("sumIf({apps}, {program} = 'CBS')", "sum(CASE WHEN program = 'CBS' THEN apps END)"),
    ("distinct_countIf({id}, {flag} <> 'N')", "count(DISTINCT CASE WHEN flag <> 'N' THEN id END)"),
    ("{Mail Qty} * 2", "`Mail Qty` * 2"),
    ("NOT {a} = 1 AND {b} = 2 OR {c} = 3", "NOT a = 1 AND b = 2 OR c = 3"),
    ("-{x} + 1.5", "-x + 1.5"),
    ("'it''s'", "'it''s'"),
    ('"double"', "'double'"),
    ("log({x})", "log10(x)"),
])
def test_translates(qs, sql):
    t = translate(qs)
    assert t.sql == sql
    assert t.needs_review == []


def test_aggregate_detection():
    assert translate("sum({a}) / count({b})").is_aggregate is True
    assert translate("{a} * 2").is_aggregate is False


def test_parameter_reference_becomes_named_parameter_and_is_flagged():
    t = translate("ifelse({year} = ${AsOfYear}, {apps}, 0)")
    assert t.sql == "CASE WHEN year = :AsOfYear THEN apps ELSE 0 END"
    assert t.parameters == ["AsOfYear"]


def test_table_calculation_is_kept_and_flagged():
    t = translate("periodOverPeriodPercentDifference(sum({apps}), {year}, YEAR, 1)")
    assert any("periodOverPeriodPercentDifference" in r for r in t.needs_review)
    assert t.is_table_calc is True


def test_unknown_function_is_flagged_not_dropped():
    t = translate("someNewFn({a})")
    assert "somenewfn(a)" in t.sql.lower()
    assert any("someNewFn" in r for r in t.needs_review)


def test_resolver_inlines_calculated_fields():
    calc = {"Resp %": "sum(apps) / sum(mail_qty)"}
    t = translate("{Resp %} * 100", resolve=lambda n: calc.get(n))
    assert t.sql == "(sum(apps) / sum(mail_qty)) * 100"


def test_parse_error_is_reported_not_raised():
    t = translate("sum({a}")
    assert t.sql == "sum({a}"
    assert t.needs_review and "parse" in t.needs_review[0].lower()


def test_partition_list_is_parsed_and_table_calc_flagged():
    t = translate("percentOfTotal(distinct_count({customer_number}), [{branch}])")
    assert not any("parse" in r for r in t.needs_review)
    assert t.is_table_calc and "[branch]" in t.sql

"""Translate QuickSight calculated-field expressions to Databricks SQL.

Covers the common scalar, conditional, string, date and simple aggregate functions.
Anything it can't translate faithfully (table calculations, level-aware aggregations,
unknown functions) is kept in the output and listed in `needs_review`, so nothing is
silently dropped. Usage:

    from qs_expr import translate
    t = translate("ifelse({amt} >= 20000, 'High', 'Low')")
    t.sql, t.needs_review, t.is_aggregate, t.parameters
"""
import re
from dataclasses import dataclass, field
from typing import Callable

# ---------------------------------------------------------------- tokenizer

TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<param>\$\{[^}]+\})
  | (?P<field>\{[^}]+\})
  | (?P<str>'(?:[^']|'')*'|"(?:[^"]|"")*")
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<op><>|!=|<=|>=|=|<|>|\+|-|\*|/|\(|\)|\[|\]|,)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
""", re.X)


class ParseError(ValueError):
    pass


def _tokens(s: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(s):
        m = TOKEN.match(s, pos)
        if not m:
            raise ParseError(f"unexpected character {s[pos]!r} at {pos}")
        pos = m.end()
        kind = m.lastgroup
        if kind != "ws":
            out.append((kind, m.group()))
    return out


# ---------------------------------------------------------------- AST + parser
# Nodes are tuples: ("bin", op, l, r) ("not", x) ("neg", x) ("call", name, [args])
#                   ("field", name) ("param", name) ("str", text) ("num", text) ("ident", name)

PREC = {"OR": 1, "AND": 2, "=": 4, "<>": 4, "!=": 4, "<": 4, ">": 4, "<=": 4, ">=": 4,
        "+": 5, "-": 5, "*": 6, "/": 6}


class _Parser:
    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self, value=None):
        tok = self.peek()
        if tok[0] is None or (value is not None and tok[1].upper() != value):
            raise ParseError(f"expected {value or 'token'}, got {tok[1]!r}")
        self.i += 1
        return tok

    def parse(self):
        node = self.expr(0)
        if self.i != len(self.t):
            raise ParseError(f"unexpected {self.peek()[1]!r}")
        return node

    def _binop(self):
        kind, val = self.peek()
        if kind == "op" and val in PREC:
            return val
        if kind == "name" and val.upper() in ("AND", "OR"):
            return val.upper()
        return None

    def expr(self, min_prec):
        left = self.unary()
        while (op := self._binop()) and PREC[op] > min_prec:
            self.i += 1
            left = ("bin", "<>" if op == "!=" else op, left, self.expr(PREC[op]))
        return left

    def unary(self):
        kind, val = self.peek()
        if kind == "name" and val.upper() == "NOT":
            self.i += 1
            return ("not", self.expr(3))
        if kind == "op" and val == "-":
            self.i += 1
            return ("neg", self.unary())
        return self.primary()

    def primary(self):
        kind, val = self.take()
        if kind == "op" and val == "(":
            node = self.expr(0)
            self.take(")")
            return ("paren", node)
        if kind == "op" and val == "[":  # partition / sort list in table calculations
            items = []
            if self.peek() != ("op", "]"):
                items.append(self.expr(0))
                while self.peek() == ("op", ","):
                    self.i += 1
                    items.append(self.expr(0))
            self.take("]")
            return ("list", items)
        if kind == "field":
            return ("field", val[1:-1])
        if kind == "param":
            return ("param", val[2:-1])
        if kind == "str":
            return ("str", val[1:-1].replace(val[0] * 2, val[0]))
        if kind == "num":
            return ("num", val)
        if kind == "name":
            if self.peek() == ("op", "("):
                self.i += 1
                args = []
                if self.peek() != ("op", ")"):
                    args.append(self.expr(0))
                    while self.peek() == ("op", ","):
                        self.i += 1
                        args.append(self.expr(0))
                self.take(")")
                return ("call", val, args)
            if val.upper() == "NULL":
                return ("null",)
            return ("ident", val)
        raise ParseError(f"unexpected {val!r}")


# ---------------------------------------------------------------- function map

DATE_UNITS = {"YYYY": "YEAR", "Q": "QUARTER", "MM": "MONTH", "WK": "WEEK", "DD": "DAY",
              "HH": "HOUR", "MI": "MINUTE", "SS": "SECOND", "MS": "MILLISECOND"}

SIMPLE = {  # QuickSight name (lower) -> Databricks SQL name, same argument order
    "sum": "sum", "avg": "avg", "min": "min", "max": "max", "count": "count",
    "median": "median", "stdev": "stddev", "stdevp": "stddev_pop", "var": "variance",
    "varp": "var_pop", "coalesce": "coalesce", "concat": "concat", "left": "left",
    "right": "right", "substring": "substring", "strlen": "length", "tolower": "lower",
    "toupper": "upper", "trim": "trim", "ltrim": "ltrim", "rtrim": "rtrim",
    "replace": "replace", "round": "round", "ceil": "ceil", "floor": "floor", "abs": "abs",
    "sqrt": "sqrt", "exp": "exp", "ln": "ln", "log": "log10", "mod": "mod",
    "startswith": "startswith", "endswith": "endswith", "split": "split_part",
    "formatdate": "date_format", "now": "current_timestamp", "greatest": "greatest",
    "least": "least", "nullif": "nullif",
}
AGGREGATES = {"sum", "avg", "min", "max", "count", "median", "stdev", "stdevp", "var", "varp",
              "distinct_count", "sumif", "avgif", "countif", "distinct_countif", "minif",
              "maxif", "percentile", "percentilecont", "percentiledisc"}
CASTS = {"tostring": "STRING", "parseint": "INT", "decimaltoint": "INT",
         "parsedecimal": "DOUBLE", "inttodecimal": "DOUBLE"}
# Table calculations / level-aware aggregations: no faithful one-to-one SQL; they become
# window functions, metric-view window measures or CTEs, decided case by case.
TABLE_CALCS = {
    "sumover", "avgover", "countover", "minover", "maxover", "distinctcountover",
    "stdevover", "varover", "rank", "denserank", "percentilerank", "percentoftotal",
    "runningsum", "runningavg", "runningcount", "runningmin", "runningmax", "difference",
    "percentdifference", "lag", "lead", "windowsum", "windowavg", "windowcount",
    "windowmin", "windowmax", "periodoverperioddifference", "periodoverperiodlastvalue",
    "periodoverperiodpercentdifference", "periodtodatesum", "periodtodateavg",
    "periodtodatecount", "periodtodatemin", "periodtodatemax", "periodtodatesumoverTime",
}
TABLE_CALCS = {n.lower() for n in TABLE_CALCS}


@dataclass
class Translation:
    sql: str
    needs_review: list[str] = field(default_factory=list)
    is_aggregate: bool = False
    is_table_calc: bool = False
    parameters: list[str] = field(default_factory=list)


def _ident(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else f"`{name.replace('`', '``')}`"


class _Emitter:
    def __init__(self, resolve):
        self.resolve = resolve
        self.review: list[str] = []
        self.agg = False
        self.table_calc = False
        self.params: list[str] = []

    def unit(self, node, default="DAY"):
        if node is None:
            return default
        raw = node[1] if node[0] in ("str", "ident") else None
        if raw and raw.upper() in DATE_UNITS:
            return DATE_UNITS[raw.upper()]
        if raw and raw.upper() in DATE_UNITS.values():
            return raw.upper()
        self.review.append(f"date unit {raw!r} not recognised")
        return self.emit(node)

    def emit(self, n, parent_prec=0) -> str:
        kind = n[0]
        if kind == "bin":
            _, op, left, right = n
            p = PREC[op]
            s = f"{self.emit(left, p)} {op} {self.emit(right, p + 1)}"
            return f"({s})" if p < parent_prec else s
        if kind == "not":
            s = f"NOT {self.emit(n[1], 3)}"
            return f"({s})" if parent_prec > 3 else s
        if kind == "neg":
            return f"-{self.emit(n[1], 7)}"
        if kind == "paren":
            return f"({self.emit(n[1])})"
        if kind == "field":
            resolved = self.resolve(n[1]) if self.resolve else None
            return f"({resolved})" if resolved else _ident(n[1])
        if kind == "param":
            self.params.append(n[1])
            return f":{n[1]}"
        if kind == "str":
            return "'" + n[1].replace("'", "''") + "'"
        if kind == "num":
            return n[1]
        if kind == "null":
            return "NULL"
        if kind == "list":
            return "[" + ", ".join(self.emit(x) for x in n[1]) + "]"
        if kind == "ident":
            return n[1]
        return self.call(n[1], n[2])

    def call(self, name: str, args: list) -> str:
        low = name.lower()
        a = [self.emit(x) for x in args]
        if low in AGGREGATES:
            self.agg = True
        if low in TABLE_CALCS:
            self.table_calc = True
            self.agg = True
            self.review.append(f"{name}: table calculation / level-aware aggregation — rebuild as a "
                               "window function, metric-view window measure or CTE")
            return f"{name}({', '.join(a)})"
        if low == "ifelse":
            if len(a) < 3 or len(a) % 2 == 0:
                self.review.append("ifelse: expected condition/value pairs plus an else value")
                return f"ifelse({', '.join(a)})"
            whens = " ".join(f"WHEN {a[i]} THEN {a[i + 1]}" for i in range(0, len(a) - 1, 2))
            return f"CASE {whens} ELSE {a[-1]} END"
        if low == "distinct_count":
            return f"count(DISTINCT {a[0]})"
        if low in ("sumif", "avgif", "countif", "minif", "maxif", "distinct_countif"):
            base = low[:-2]
            inner = f"CASE WHEN {a[1]} THEN {a[0]} END"
            return f"count(DISTINCT {inner})" if base == "distinct_count" else f"{base}({inner})"
        if low in ("isnull", "isnotnull"):
            return f"({a[0]} IS {'NOT ' if low == 'isnotnull' else ''}NULL)"
        if low in CASTS:
            return f"CAST({a[0]} AS {CASTS[low]})"
        if low == "datediff":
            unit = self.unit(args[2] if len(args) > 2 else None)
            return f"timestampdiff({unit}, {a[0]}, {a[1]})"
        if low == "truncdate":
            return f"date_trunc('{self.unit(args[0])}', {a[1]})"
        if low == "adddatetime":
            return f"timestampadd({self.unit(args[1])}, {a[0]}, {a[2]})"
        if low == "extract":
            return f"extract({self.unit(args[0])} FROM {a[1]})"
        if low == "parsedate":
            self.review.append("parseDate: check the format string (Joda vs Spark datetime patterns)")
            return f"to_timestamp({', '.join(a)})"
        if low == "locate":  # QS: locate(expression, substring[, start]); SQL: locate(substr, str[, pos])
            return f"locate({a[1]}, {a[0]}{', ' + a[2] if len(a) > 2 else ''})"
        if low == "contains":
            if len(args) > 2 and args[2][0] == "ident" and args[2][1].upper() == "CASE_INSENSITIVE":
                return f"contains(lower({a[0]}), lower({a[1]}))"
            return f"contains({a[0]}, {a[1]})"
        if low in ("percentile", "percentilecont"):
            return f"percentile_cont({a[1]} / 100.0) WITHIN GROUP (ORDER BY {a[0]})"
        if low == "percentiledisc":
            return f"percentile_disc({a[1]} / 100.0) WITHIN GROUP (ORDER BY {a[0]})"
        if low in SIMPLE:
            return f"{SIMPLE[low]}({', '.join(a)})"
        self.review.append(f"{name}: no known Databricks SQL equivalent — translate by hand")
        return f"{name}({', '.join(a)})"


def translate(expr: str, resolve: Callable[[str], str | None] | None = None) -> Translation:
    """Translate one QuickSight expression. `resolve(field_name)` may return SQL to inline
    (e.g. another calculated field's translation); return None to keep it as a column."""
    try:
        tree = _Parser(_tokens(expr)).parse()
    except ParseError as e:
        return Translation(sql=expr, needs_review=[f"could not parse expression: {e}"])
    em = _Emitter(resolve)
    sql = em.emit(tree)
    return Translation(sql=sql, needs_review=em.review, is_aggregate=em.agg,
                       is_table_calc=em.table_calc, parameters=em.params)

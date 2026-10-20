"""
Shared KPI engine. Every delivery channel uses this module, so a KPI is calculated
the same way everywhere:

  * gold-table SQL (DuckDB build, Spark notebook)  -> kpi_sql()
  * Tableau / Power BI calculated fields            -> kpi_tableau() / kpi_dax()
  * pandas (Streamlit embedded app, tests)          -> compute_kpis()

Formulas live in metrics/kpis.yml. KPIs may only reference components, which keeps
every KPI computable from the gold tables at any grain.
"""
from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path

import pandas as pd
import yaml

SPEC_PATH = Path(__file__).with_name("kpis.yml")
_IDENT = re.compile(r"\b[a-z_][a-z0-9_]*\b")


@lru_cache(maxsize=1)
def spec() -> dict:
    s = yaml.safe_load(SPEC_PATH.read_text())
    s["kpi_by_name"] = {k["name"]: k for k in s["kpis"]}
    _validate(s)
    return s


def components() -> dict[str, dict]:
    return spec()["components"]


def kpi(name: str) -> dict:
    return spec()["kpi_by_name"][name]


def kpi_names(level: str | None = None) -> list[str]:
    return [k["name"] for k in spec()["kpis"] if level is None or k["level"] == level]


def component_select_sql(indent: str = "    ") -> str:
    """The aggregate SELECT list shared by every gold table (DuckDB and Spark SQL)."""
    return (",\n" + indent).join(f"{c['sql']} as {name}" for name, c in components().items())


# ------------------------------------------------------------------ formula helpers
def _exprs(k: dict) -> tuple[str, str | None]:
    if k["type"] == "ratio":
        return k["numerator"], k["denominator"]
    return k["expr"], None


def _idents(expr: str) -> set[str]:
    return set(_IDENT.findall(expr))


def _validate(s: dict) -> None:
    comps = set(s["components"])
    for k in s["kpis"]:
        for e in filter(None, _exprs(k)):
            unknown = _idents(e) - comps
            if unknown:
                raise ValueError(f"KPI {k['name']} references non-components {sorted(unknown)}")
            tree = ast.parse(e, mode="eval")
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load,
                                         ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub)):
                    raise ValueError(f"KPI {k['name']}: only + - * / allowed, found {type(node).__name__}")


def _wrap(expr: str, fmt: str) -> str:
    return _IDENT.sub(lambda m: fmt.format(m.group(0)), expr)


def kpi_sql(name: str) -> str:
    """SQL expression over a gold table (works at any GROUP BY grain)."""
    k = kpi(name)
    num, den = _exprs(k)
    n = _wrap(num, "sum({})")
    if k["type"] == "ratio":
        return f"({n}) / nullif({_wrap(den, 'sum({})')}, 0)"
    if k["type"] == "share_of_total":
        return f"({n}) / nullif(sum({n}) over (partition by {{partition}}), 0)"
    return n


def kpi_tableau(name: str) -> str:
    k = kpi(name)
    num, den = _exprs(k)
    n = _wrap(num, "SUM([{}])")
    if k["type"] == "ratio":
        return f"({n}) / ZN({_wrap(den, 'SUM([{}])')})"
    if k["type"] == "share_of_total":
        return f"({n}) / TOTAL({n})   // table calculation: compute using [company]"
    return n


def kpi_dax(name: str, table: str = "daily_zone_company") -> str:
    k = kpi(name)
    num, den = _exprs(k)
    n = _wrap(num, f"SUM({table}[{{}}])")
    if k["type"] == "ratio":
        return f"DIVIDE({n}, {_wrap(den, f'SUM({table}[{{}}])')})"
    if k["type"] == "share_of_total":
        return f"DIVIDE({n}, CALCULATE({n}, ALL({table}[company])))"
    return n


def _eval(expr: str, frame: pd.DataFrame) -> pd.Series:
    env = {c: frame[c].astype("float64") for c in _idents(expr)}
    return eval(compile(ast.parse(expr, mode="eval"), "<kpi>", "eval"), {"__builtins__": {}}, env)


def compute_kpis(df: pd.DataFrame, by: list[str] | None = None, names: list[str] | None = None) -> pd.DataFrame:
    """Aggregate component columns to the `by` grain, then compute KPIs.

    df must contain the component columns (any finer grain is fine).
    share_of_total KPIs need 'company' in df; they are computed across companies within
    each slice of the remaining `by` columns.
    """
    by = list(by or [])
    names = names or kpi_names()
    comps = [c for c in components() if c in df.columns]
    agg = df.groupby(by, dropna=False)[comps].sum().reset_index() if by else df[comps].sum().to_frame().T
    out = agg[by].copy() if by else pd.DataFrame(index=agg.index)
    for name in names:
        k = kpi(name)
        num, den = _exprs(k)
        n = _eval(num, agg)
        if k["type"] == "ratio":
            d = _eval(den, agg)
            out[name] = (n / d.where(d != 0)).astype("float64")
        elif k["type"] == "share_of_total":
            other = [c for c in by if c != k["share_across"]]
            total = n.groupby([agg[c] for c in other]).transform("sum") if other else n.sum()
            out[name] = (n / total).astype("float64")
        else:
            out[name] = n.astype("float64")
    return out


def fmt(name: str, value: float) -> str:
    if value is None or pd.isna(value):
        return "—"
    f = kpi(name)["format"]
    return {"int": f"{value:,.0f}", "usd": f"${value:,.0f}", "usd2": f"${value:,.2f}",
            "pct": f"{value:.1%}", "dec2": f"{value:,.2f}"}[f]

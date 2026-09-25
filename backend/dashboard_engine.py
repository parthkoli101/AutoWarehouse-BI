"""
dashboard_engine.py

Generates MULTIPLE themed dashboards from one warehouse, not a single
fixed set of KPIs/charts -- appropriate for a warehouse built from a
whole folder of files (6-10+ tables), where one flat dashboard can't
meaningfully cover everything.

Flow:
  1. build_schema_context()   -- real table/column/sample data from DuckDB
  2. _propose_themes()        -- one Ollama call: what are the natural
                                  focus areas in THIS schema? (e.g. "Sales",
                                  "Customer Support", "Delivery Performance")
  3. _design_theme()          -- one Ollama call PER theme: KPIs + charts
                                  scoped to that theme, as real SQL
  4. _generate_multi_insights() -- ONE combined Ollama call, fed the real
                                  computed numbers from every theme, grounds
                                  every insight in what actually happened
  5. _build_centralized_dashboard() -- deterministic (no LLM): pulls the
                                  top KPIs/charts from each theme into one
                                  "Centralized Overview" -- fast and never
                                  hallucinated, since it's just selection
  6. design_dashboards()      -- orchestrates all of the above, saves it
  7. refresh_dashboards()     -- re-runs every saved query across every
                                  dashboard, no LLM call -- the manual
                                  "Refresh" button

Safety: every query Ollama generates (for any dashboard, any chart type)
is treated as untrusted input -- single read-only SELECT, no other
statement types, table names checked against what's actually in the
warehouse. Chart shape (row/cell counts, pie-slice limits, heatmap sizing)
and KPI formatting are sanitized server-side, not just prompted for.
"""

import json
import re
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal

import duckdb

from ollama_client import generate_json, OllamaError

logger = logging.getLogger("dashboard_engine")

FORBIDDEN_KEYWORDS = [
    "drop", "delete", "update", "insert", "alter", "attach", "detach",
    "pragma", "copy", "create", "export", "import", "call", "vacuum",
    "checkpoint", "install", "load",
]

MAX_ROWS_PER_QUERY = 500
MAX_CHART_ROWS = 10        # bar/line/pie: never render more than this many bars/slices
MAX_PIE_SLICES = 6
MAX_HEATMAP_CELLS = 64     # roughly an 8x8 grid -- readable, still detailed
MAX_THEMES = 6
MIN_THEMES = 2
KPIS_PER_THEME = 5
CHARTS_PER_THEME = 6
VALID_CHART_TYPES = {"bar", "line", "pie", "heatmap", "table"}


def _config_path(db_path: Path) -> Path:
    db_path = Path(db_path)
    return db_path.parent / f"{db_path.stem}_dashboard_config.json"


def _get_user_tables(con) -> list:
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()]
    return [t for t in tables if not t.startswith("_warehouse_")]


def build_schema_context(db_path: Path) -> tuple:
    """Returns (context_text, allowed_tables). Real schema, real row
    counts, real sample rows. ID-like columns explicitly flagged as
    unsuitable for chart labels."""
    con = duckdb.connect(str(db_path), read_only=True)
    tables = _get_user_tables(con)

    if not tables:
        con.close()
        return "", []

    blocks = []
    for t in tables:
        cols = con.execute(f'DESCRIBE "{t}"').fetchall()
        col_desc_parts = []
        for c in cols:
            col_name, col_type = c[0], c[1]
            is_id_col = col_name.lower() == "id" or col_name.lower().endswith("_id")
            tag = " [ID -- do not use as a chart label/x_field, opaque key not human-readable]" if is_id_col else ""
            col_desc_parts.append(f"{col_name} ({col_type}){tag}")
        col_desc = ", ".join(col_desc_parts)
        row_count = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]

        sample = con.execute(f'SELECT * FROM "{t}" LIMIT 2').fetchall()
        col_names = [c[0] for c in cols]
        sample_text = "\n".join(
            "  " + ", ".join(f"{col_names[i]}={row[i]}" for i in range(len(col_names)))
            for row in sample
        )

        blocks.append(f"TABLE {t} ({row_count:,} rows)\nCOLUMNS: {col_desc}\nSAMPLE ROWS:\n{sample_text}")

    con.close()
    return "\n\n".join(blocks), tables


# ---------------------------------------------------------------------------
# SQL SAFETY (unchanged posture: untrusted input, single SELECT only)
# ---------------------------------------------------------------------------
def _validate_sql(sql: str, allowed_tables: list) -> tuple:
    if not sql or not isinstance(sql, str):
        return False, "empty or non-string query"

    cleaned = sql.strip().rstrip(";").strip()

    if not re.match(r"^\s*SELECT\b", cleaned, re.IGNORECASE):
        return False, "query does not start with SELECT"
    if ";" in cleaned:
        return False, "multiple statements not allowed"

    lowered = cleaned.lower()
    for kw in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", lowered):
            return False, f"forbidden keyword '{kw}' found"

    if allowed_tables and not any(re.search(rf"\b{re.escape(t)}\b", cleaned, re.IGNORECASE) for t in allowed_tables):
        return False, "query doesn't reference any known warehouse table"

    return True, None


def _json_safe(value):
    """DuckDB can return types Flask's default JSON encoder can't handle --
    most notably datetime.timedelta from date subtraction (e.g. AVG(end -
    start) for a duration metric), plus date/datetime/Decimal in general.
    Coerce anything non-JSON-native into a safe representation, done once
    here so every downstream consumer (KPIs, charts, insights) is
    automatically protected rather than each caller needing to know about it."""
    if isinstance(value, timedelta):
        return round(value.total_seconds() / 86400, 2)  # express as fractional days -- the common case (order/delivery duration etc)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _execute_query(con, sql: str) -> list:
    cleaned = sql.strip().rstrip(";").strip()
    result = con.execute(cleaned)
    col_names = [d[0] for d in result.description]
    rows = result.fetchmany(MAX_ROWS_PER_QUERY)
    return [{col: _json_safe(val) for col, val in zip(col_names, row)} for row in rows]


# ---------------------------------------------------------------------------
# CHART SANITIZATION -- field-name repair, row/cell capping, type safety
# ---------------------------------------------------------------------------
def _is_numeric(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return False  # bool is technically int in Python, but not a meaningful chart value
    if isinstance(value, (int, float)):
        return True
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _fix_chart_fields(chart: dict) -> dict:
    """Root-cause fix for charts silently rendering broken: either an
    unaliased SQL aggregate (e.g. bare COUNT(*)) comes back under an
    internal DuckDB name that matches neither x_field/y_field the model
    guessed, OR the model assigned a categorical column as the VALUE axis
    (e.g. y_field holding a segment name like "Regular" instead of a
    count) -- text can't be plotted as a bar height. Verify names exist,
    repair positionally if not, and if the value axis isn't actually
    numeric, try swapping x/y (the model may have reversed them) before
    giving up. All of this is pure type/structure inspection of whatever
    the query actually returned -- no dataset-specific logic anywhere."""
    rows = chart.get("data", [])
    if not rows:
        return chart

    actual_columns = list(rows[0].keys())
    is_heatmap = chart.get("chart_type") == "heatmap"

    if is_heatmap:
        x_field, y_field, value_field = chart.get("x_field"), chart.get("y_field"), chart.get("value_field")
        fields_ok = all(f in actual_columns for f in (x_field, y_field, value_field))
        if not fields_ok:
            if len(actual_columns) >= 3:
                fixed = actual_columns[:3]
                logger.warning(
                    f"Chart '{chart.get('title')}' (heatmap): field mismatch "
                    f"(wanted x={x_field!r}, y={y_field!r}, value={value_field!r}; got {actual_columns}) "
                    f"-- repaired positionally to {fixed}"
                )
                chart["x_field"], chart["y_field"], chart["value_field"] = fixed
            else:
                logger.warning(f"Chart '{chart.get('title')}': heatmap needs 3 columns, got {actual_columns} -- converting to bar")
                chart["chart_type"] = "bar"
                if len(actual_columns) >= 2:
                    chart["x_field"], chart["y_field"] = actual_columns[0], actual_columns[1]
        return chart

    if chart.get("chart_type") == "table":
        return chart  # raw columns, no x/y concept to fix

    x_field, y_field = chart.get("x_field"), chart.get("y_field")
    x_ok, y_ok = x_field in actual_columns, y_field in actual_columns
    if not x_ok or not y_ok:
        if len(actual_columns) >= 2:
            fixed_x = actual_columns[0] if not x_ok else x_field
            fixed_y = actual_columns[1] if not y_ok else y_field
            logger.warning(
                f"Chart '{chart.get('title')}': field mismatch "
                f"(wanted x={x_field!r}, y={y_field!r}; got {actual_columns}) "
                f"-- repaired positionally to x={fixed_x!r}, y={fixed_y!r}"
            )
            chart["x_field"], chart["y_field"] = fixed_x, fixed_y
            x_field, y_field = fixed_x, fixed_y

    # value axis must be numeric -- a categorical string (e.g. a segment
    # name) can't be plotted as a bar height. If y isn't numeric but x is,
    # the model likely had them backwards -- swap rather than discard.
    y_numeric = all(_is_numeric(r.get(y_field)) for r in rows if r.get(y_field) is not None)
    if not y_numeric:
        x_numeric = all(_is_numeric(r.get(x_field)) for r in rows if r.get(x_field) is not None)
        if x_numeric:
            logger.warning(f"Chart '{chart.get('title')}': y_field '{y_field}' isn't numeric but x_field '{x_field}' is -- swapping axes")
            chart["x_field"], chart["y_field"] = y_field, x_field
        else:
            chart["_invalid_reason"] = f"neither '{x_field}' nor '{y_field}' contains numeric values -- cannot plot as a chart"

    return chart


def _sanitize_chart(chart: dict) -> dict:
    """Backend safety net -- does not trust the model to have followed the
    prompt's row/cell-count rules, its own claimed field names, or that it
    picked a genuinely numeric value column."""
    chart = _fix_chart_fields(chart)
    rows = chart.get("data", [])
    chart_type = chart.get("chart_type", "bar")

    if chart_type == "table":
        if len(rows) > MAX_CHART_ROWS:
            rows = rows[:MAX_CHART_ROWS]
        chart["data"] = rows
        return chart

    if chart_type == "heatmap":
        x_field, y_field, value_field = chart.get("x_field"), chart.get("y_field"), chart.get("value_field")
        # aggregate (sum) any rows sharing the same (x,y) cell BEFORE
        # capping -- otherwise duplicate cells (e.g. a GROUP BY that wasn't
        # fully collapsed) render as overlapping, illegible stacked labels
        # in the same grid position instead of one clean number.
        if x_field and y_field and value_field and rows:
            aggregated = {}
            for r in rows:
                key = (r.get(x_field), r.get(y_field))
                val = r.get(value_field) or 0
                aggregated[key] = aggregated.get(key, 0) + (val if _is_numeric(val) else 0)
            rows = [{x_field: k[0], y_field: k[1], value_field: v} for k, v in aggregated.items()]
            rows.sort(key=lambda r: r[value_field], reverse=True)
        if len(rows) > MAX_HEATMAP_CELLS:
            rows = rows[:MAX_HEATMAP_CELLS]
        chart["data"] = rows
        return chart

    y_field = chart.get("y_field")
    if y_field and rows and all(y_field in r for r in rows):
        try:
            rows = sorted(rows, key=lambda r: (r[y_field] if r[y_field] is not None else 0), reverse=True)
        except TypeError:
            pass

    if len(rows) > MAX_CHART_ROWS:
        rows = rows[:MAX_CHART_ROWS]

    if chart_type == "pie" and len(rows) > MAX_PIE_SLICES:
        chart_type = "bar"

    chart["data"] = rows
    chart["chart_type"] = chart_type
    return chart


def _sanitize_kpi_format(kpi: dict) -> dict:
    value = kpi.get("value")
    title = (kpi.get("title") or "").lower()
    fmt = kpi.get("format", "number")

    if fmt == "percent" and value is not None:
        looks_like_rating = any(word in title for word in ["score", "rating", "star"])
        implausible_percent = isinstance(value, (int, float)) and 0 <= value <= 10 and looks_like_rating
        if implausible_percent:
            kpi["format"] = "number"
            if "out of" not in title:
                kpi["title"] = f"{kpi['title']} (out of 5)"
    return kpi


# ---------------------------------------------------------------------------
# STEP 1: propose dashboard themes
# ---------------------------------------------------------------------------
def _build_theme_prompt(schema_context: str) -> str:
    return f"""You are a senior data analyst planning a MULTI-DASHBOARD analytics
suite for a data warehouse. Below is the REAL schema -- every table, every
column, real row counts, real sample values.

{schema_context}

Identify {MIN_THEMES} to {MAX_THEMES} natural, DISTINCT focus areas this data
actually supports. Think like an analyst scoping a BI project, not just
listing table names:
- Look at every table, not just the largest one -- a small table (e.g.
  reviews, stores, a date dimension) often unlocks its own theme (customer
  experience, geography/operations, seasonality) when joined with a larger
  one.
- The number of themes should reflect the actual BREADTH of the schema: a
  warehouse with many distinct entities (customers, products, transactions,
  feedback, locations, time) supports MORE themes than one with only two or
  three tables. Do not default to the minimum just because it's safe --
  use as many of the {MIN_THEMES}-{MAX_THEMES} themes as the real breadth of
  tables/columns above actually supports. Only return fewer than {MAX_THEMES}
  if the schema genuinely doesn't have that many distinct angles.
- Typical angles worth checking for (only if the schema actually supports
  them): transaction volume/value, customer behavior/segmentation,
  product/catalog performance, quality or satisfaction signals (ratings,
  returns, complaints), geography/location performance, operational
  timing (any pair of "start" and "end"/"completed" style date columns
  implies a duration/fulfillment-speed theme), and time-based seasonality
  if a date dimension or date columns exist.
- Base every theme ONLY on tables/columns that actually exist above -- do
  not invent a theme with no supporting data.

Respond with ONLY valid JSON in exactly this shape, no other text:
{{
  "themes": [
    {{"title": "short theme name, e.g. 'Sales Performance'", "focus": "one sentence on what this dashboard should analyze, grounded in the real tables/columns above"}}
  ]
}}
"""


def _propose_themes(schema_context: str, model: str = None) -> list:
    kwargs = {"model": model} if model else {}
    raw = generate_json(_build_theme_prompt(schema_context), **kwargs)
    themes = raw.get("themes", [])

    cleaned, seen_titles = [], set()
    for t in themes:
        title = str(t.get("title", "")).strip()
        focus = str(t.get("focus", "")).strip()
        if not title or title.lower() in seen_titles:
            continue
        seen_titles.add(title.lower())
        cleaned.append({"title": title, "focus": focus})
        if len(cleaned) >= MAX_THEMES:
            break

    if len(cleaned) < MIN_THEMES:
        cleaned = cleaned or [{"title": "Overview", "focus": "General overview of the warehouse."}]
    return cleaned


# ---------------------------------------------------------------------------
# STEP 2: design one theme's dashboard (KPIs + charts, real SQL)
# ---------------------------------------------------------------------------
def _build_design_prompt(schema_context: str, theme_title: str, theme_focus: str) -> str:
    return f"""You are designing ONE dashboard within a larger multi-dashboard suite.
This dashboard's theme is: "{theme_title}" -- {theme_focus}

Below is the REAL schema for the whole warehouse (use only what's relevant
to THIS theme; other dashboards cover other angles). Columns tagged [ID]
are opaque keys -- never use them as a chart label/x_field, only for joins.

{schema_context}

Propose {KPIS_PER_THEME} KPI numbers and {CHARTS_PER_THEME} charts specifically
for the "{theme_title}" theme. Stay focused on this theme -- do not drift into
unrelated areas the other dashboards in this suite would cover. Do not propose
two KPIs/charts that are trivial restatements of each other.

Think like a data analyst, not a script that counts rows in one table at a
time:
- PREFER metrics that JOIN two or more tables when the schema supports it
  over a trivial single-table count -- e.g. combine a transaction table with
  the entity tables it references (who/what/where) rather than reporting
  transaction volume alone.
- If the schema has two date-like columns that represent a start and an
  end/completion of the same process (e.g. one date column for when
  something was initiated and another for when it was completed/fulfilled/
  resolved, on the same or joined tables), consider a derived DURATION
  metric (e.g. AVG(end_date - start_date)) -- this is usually a genuinely
  valuable operational metric that a single-table view would miss entirely.
- If a quality/feedback-style table exists (ratings, reviews, complaints,
  returns) and it can be joined to an entity table (product, seller,
  location, category), consider combining them -- e.g. average rating BY
  category, not just average rating overall.
- If there's a table that looks like a date dimension or has clear calendar
  attributes (month, quarter, season, day-of-week, is_weekend, etc.),
  consider a chart that breaks a metric down by one of those attributes
  instead of only by raw date.
- Only do this where the actual columns above genuinely support it -- never
  invent a join key or column that isn't in the schema shown.

For each, write a single DuckDB SQL SELECT query computing it directly from
the tables above (exact table/column names -- do not invent columns).

Rules for every query:
- Must be a single SELECT statement only. No other SQL statement types.
- No semicolons, no comments, no subqueries that modify data.
- ALWAYS explicitly alias every computed/aggregate column with AS, and that
  alias MUST exactly match what you put in x_field/y_field (or x_field/
  y_field/value_field for heatmap). An unaliased aggregate returns an
  unusable internal column name and the chart will show no data.
- NEVER use an [ID]-tagged column as a chart's x_field/label.
- If a chart's grouping could produce more than ~8 rows, ORDER BY the value
  descending and LIMIT to the top 8.

Chart type guide -- use whichever genuinely fits the data, not always bar:
- "bar": category vs. value comparisons, most common choice.
- "line": a value over time (dates, months, ordered days).
- "pie": ONLY when the result has 6 or fewer categories.
- "heatmap": when you have TWO categorical dimensions and ONE aggregate
  value across their combination (e.g. state x category -> total revenue,
  or day_of_week x hour -> order_count). Requires GROUP BY on both
  categorical columns together. For heatmap, set "x_field", "y_field", AND
  "value_field" (three fields, not two) to the exact aliases used in the
  SQL. Only propose a heatmap if this theme's tables genuinely have two
  meaningful categorical dimensions to cross -- don't force one.
- "table": a ranked leaderboard, when an entity has a name/identifier PLUS
  2 or more other relevant columns worth showing together (e.g. a
  product's name, category, units sold, and revenue all in one row) --
  more informative than forcing that into a single bar chart. Select 4-6
  columns total including the name column, ORDER BY the most important
  metric DESC, LIMIT 10. For "table", do NOT set x_field/y_field/
  value_field at all -- every selected column is shown as its own table
  column, using the exact SQL aliases as headers.

CRITICAL rule for every KPI and every bar/line/pie/heatmap chart's value
column: it MUST be a genuine number produced by an aggregate (COUNT, SUM,
AVG, MIN, MAX, or arithmetic on those) -- NEVER a raw category, name, or
status string. If you want to highlight which category is "best" (e.g.
the top city, the top segment), that belongs in a "table" leaderboard or
as the label (x_field) of a bar chart, never as the numeric value itself.

Rules for KPI format:
- "currency" -- monetary totals/sums only.
- "percent" -- ONLY for values already expressed 0-100 (e.g. 100.0 * a/b).
  Never use "percent" for a bounded rating (e.g. 1-5 stars) -- use "number"
  and put the scale in the title, e.g. "Average Rating (out of 5)".
- "number" -- counts, averages, anything else.

Respond with ONLY valid JSON in exactly this shape, no other text:
{{
  "kpis": [{{"title": "string", "sql": "SELECT ...", "format": "number|currency|percent"}}],
  "charts": [
    {{"title": "string", "chart_type": "bar|line|pie", "sql": "SELECT ...", "x_field": "...", "y_field": "..."}},
    {{"title": "string", "chart_type": "heatmap", "sql": "SELECT ...", "x_field": "...", "y_field": "...", "value_field": "..."}},
    {{"title": "string", "chart_type": "table", "sql": "SELECT name_col, metric1, metric2, ... FROM ... ORDER BY ... DESC LIMIT 10"}}
  ]
}}
"""


def _design_theme(con, allowed_tables: list, schema_context: str, theme: dict, model: str = None) -> dict:
    kwargs = {"model": model} if model else {}
    raw = generate_json(_build_design_prompt(schema_context, theme["title"], theme["focus"]), **kwargs)

    kpis_out, charts_out, rejected = [], [], []

    for kpi in raw.get("kpis", []):
        sql = kpi.get("sql", "")
        ok, reason = _validate_sql(sql, allowed_tables)
        if not ok:
            rejected.append({"title": kpi.get("title"), "sql": sql, "reason": reason})
            continue
        try:
            rows = _execute_query(con, sql)
            value = list(rows[0].values())[0] if rows else None
            # A KPI headline must be a real number -- a categorical string
            # (e.g. a segment name like "Regular") can't be shown as a
            # metric. This check is purely about the returned value's
            # TYPE, never about what the value actually is.
            if value is not None and not _is_numeric(value):
                rejected.append({"title": kpi.get("title"), "sql": sql,
                                  "reason": f"KPI value is not numeric (got {value!r}) -- a KPI must be a real number"})
                continue
            kpis_out.append({"title": kpi.get("title", "KPI"), "sql": sql, "format": kpi.get("format", "number"), "value": value})
        except Exception as e:
            rejected.append({"title": kpi.get("title"), "sql": sql, "reason": f"execution error: {e}"})

    for chart in raw.get("charts", []):
        sql = chart.get("sql", "")
        ok, reason = _validate_sql(sql, allowed_tables)
        if not ok:
            rejected.append({"title": chart.get("title"), "sql": sql, "reason": reason})
            continue
        chart_type = chart.get("chart_type", "bar")
        if chart_type not in VALID_CHART_TYPES:
            chart_type = "bar"
        try:
            rows = _execute_query(con, sql)
            base = {
                "title": chart.get("title", "Chart"), "chart_type": chart_type, "sql": sql,
                "x_field": chart.get("x_field"), "y_field": chart.get("y_field"), "data": rows,
            }
            if chart_type == "heatmap":
                base["value_field"] = chart.get("value_field")
            sanitized = _sanitize_chart(base)
            if sanitized.get("_invalid_reason"):
                rejected.append({"title": chart.get("title"), "sql": sql, "reason": sanitized["_invalid_reason"]})
                continue
            charts_out.append(sanitized)
        except Exception as e:
            rejected.append({"title": chart.get("title"), "sql": sql, "reason": f"execution error: {e}"})

    kpis_out = [_sanitize_kpi_format(k) for k in kpis_out]

    if rejected:
        logger.warning(f"Theme '{theme['title']}': {len(rejected)} quer(y/ies) rejected: {rejected}")
    empty = [c["title"] for c in charts_out if len(c["data"]) == 0]
    if empty:
        logger.warning(f"Theme '{theme['title']}': {len(empty)} chart(s) returned zero rows: {empty}")

    return {"title": theme["title"], "focus": theme["focus"], "kpis": kpis_out, "charts": charts_out,
            "rejected_count": len(rejected), "empty_chart_count": len(empty)}


# ---------------------------------------------------------------------------
# STEP 3: combined grounded insights, ONE call for every theme together
# ---------------------------------------------------------------------------
def _chart_summary_line(c: dict) -> str:
    if c["chart_type"] == "heatmap":
        top = c["data"][:5]
        cells = "; ".join(f"({r.get(c['x_field'])},{r.get(c['y_field'])})={r.get(c['value_field'])}" for r in top)
        return f"- {c['title']} (heatmap): {cells}"
    if c["chart_type"] == "table":
        top = c["data"][:5]
        rows_text = "; ".join(", ".join(f"{k}={v}" for k, v in r.items()) for r in top)
        return f"- {c['title']} (leaderboard table): {rows_text}"
    top = c["data"][:5]
    rows_text = "; ".join(f"{r.get(c['x_field'])}={r.get(c['y_field'])}" for r in top)
    return f"- {c['title']} ({c['chart_type']}): {rows_text}"


def _build_multi_insight_prompt(theme_results: list) -> str:
    sections = []
    for t in theme_results:
        kpi_lines = "\n".join(f"  - {k['title']}: {k['value']}" for k in t["kpis"])
        chart_lines = "\n".join("  " + _chart_summary_line(c) for c in t["charts"])
        sections.append(f'THEME "{t["title"]}"\nKPIs:\n{kpi_lines}\nCharts:\n{chart_lines}')
    all_text = "\n\n".join(sections)

    return f"""You are a business analyst. Below are REAL computed results from
several themed dashboards in one data warehouse -- not hypothetical, these
are the actual current numbers.

{all_text}

For EACH theme, write a grounded explanation referencing its exact numbers
(compare values, call out concentration/outliers). Do not restate the raw
list -- synthesize it into insight a manager would find useful. Keep each
theme's insights scoped to that theme only.

Respond with ONLY valid JSON in exactly this shape, no other text:
{{
  "themes": [
    {{
      "title": "must match a theme title exactly",
      "summary": "3-4 sentence narrative for this theme, referencing its real numbers",
      "kpi_insights": [{{"title": "must match a KPI title exactly", "insight": "one grounded sentence"}}],
      "chart_insights": [{{"title": "must match a chart title exactly", "insight": "one grounded sentence"}}]
    }}
  ]
}}
"""


def _generate_multi_insights(theme_results: list, model: str = None) -> dict:
    """Returns {theme_title: {"summary":..., "kpi_insights":{...}, "chart_insights":{...}}}.
    Best-effort -- if this single call fails, every dashboard still works
    with numbers/charts, just without narrative text."""
    try:
        kwargs = {"model": model} if model else {}
        raw = generate_json(_build_multi_insight_prompt(theme_results), **kwargs)
        out = {}
        for t in raw.get("themes", []):
            title = t.get("title")
            if not title:
                continue
            out[title] = {
                "summary": t.get("summary", ""),
                "kpi_insights": {i["title"]: i["insight"] for i in t.get("kpi_insights", []) if "title" in i and "insight" in i},
                "chart_insights": {i["title"]: i["insight"] for i in t.get("chart_insights", []) if "title" in i and "insight" in i},
            }
        return out
    except OllamaError as e:
        logger.warning(f"Combined insight generation failed (dashboards still valid): {e}")
        return {}


# ---------------------------------------------------------------------------
# STEP 4: centralized overview -- deterministic, no LLM call
# ---------------------------------------------------------------------------
def _build_centralized_dashboard(theme_results: list) -> dict:
    """Pulls the top KPIs/charts from every theme into one combined view.
    Deliberately NOT an LLM call: pure selection over data that's already
    validated and grounded, so it's fast and can never hallucinate."""
    kpis, charts = [], []
    for t in theme_results:
        kpis.extend(t["kpis"][:2])       # top 2 KPIs per theme
        if t["charts"]:
            charts.append(t["charts"][0])  # one representative chart per theme

    theme_names = ", ".join(t["title"] for t in theme_results)
    summary = (
        f"Centralized view combining the most important metrics across {len(theme_results)} "
        f"focus area(s): {theme_names}. Each section below has its own dedicated dashboard "
        f"with deeper detail."
    )
    return {"title": "Centralized Overview", "focus": "Top metrics across every dashboard", "id": "centralized",
            "kpis": kpis, "charts": charts, "summary": summary}


# ---------------------------------------------------------------------------
# TOP-LEVEL: design / refresh the full multi-dashboard suite
# ---------------------------------------------------------------------------
def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "dashboard"


def design_dashboards(db_path: Path, model: str = None) -> dict:
    """Full multi-dashboard design pass. Returns the same shape saved to
    disk, so refresh_dashboards() can reproduce it without any LLM call."""
    schema_context, allowed_tables = build_schema_context(db_path)
    if not allowed_tables:
        raise ValueError("Warehouse is empty -- build a warehouse before designing dashboards.")

    logger.info("Proposing dashboard themes for this schema...")
    themes = _propose_themes(schema_context, model)
    logger.info(f"Themes: {[t['title'] for t in themes]}")

    con = duckdb.connect(str(db_path), read_only=True)
    theme_results = []
    for theme in themes:
        logger.info(f"Designing dashboard for theme '{theme['title']}'...")
        theme_results.append(_design_theme(con, allowed_tables, schema_context, theme, model))
    con.close()

    logger.info("Generating grounded insights across all themes (one combined call)...")
    insights_by_theme = _generate_multi_insights(theme_results, model)

    dashboards = []
    for t in theme_results:
        ins = insights_by_theme.get(t["title"], {"summary": "", "kpi_insights": {}, "chart_insights": {}})
        for k in t["kpis"]:
            k["insight"] = ins["kpi_insights"].get(k["title"], "")
        for c in t["charts"]:
            c["insight"] = ins["chart_insights"].get(c["title"], "")
        dashboards.append({
            "id": _slugify(t["title"]), "title": t["title"], "focus": t["focus"],
            "kpis": t["kpis"], "charts": t["charts"], "summary": ins["summary"],
        })

    centralized = _build_centralized_dashboard(theme_results)
    all_dashboards = [{**centralized, "id": "centralized"}] + dashboards

    design = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dashboards": [
            {
                "id": d["id"], "title": d["title"], "focus": d.get("focus", ""),
                "kpis": [{"title": k["title"], "sql": k["sql"], "format": k["format"]} for k in d["kpis"] if "sql" in k],
                "charts": [
                    {k: c[k] for k in ("title", "chart_type", "sql", "x_field", "y_field", "value_field") if k in c}
                    for c in d["charts"]
                ],
                "summary": d.get("summary", ""),
                "kpi_insights": {k["title"]: k.get("insight", "") for k in d["kpis"] if "sql" in k},
                "chart_insights": {c["title"]: c.get("insight", "") for c in d["charts"] if "sql" in c},
            }
            for d in all_dashboards
        ],
    }
    _config_path(db_path).write_text(json.dumps(design, indent=2))

    total_rejected = sum(t["rejected_count"] for t in theme_results)
    total_empty = sum(t["empty_chart_count"] for t in theme_results)

    return {
        "generated_at": design["generated_at"],
        "dashboards": all_dashboards,
        "rejected_count": total_rejected,
        "empty_chart_count": total_empty,
    }


def refresh_dashboards(db_path: Path) -> dict:
    """Re-runs every saved query across every dashboard -- no LLM call.
    This is the manual 'Refresh' button."""
    config_file = _config_path(db_path)
    if not config_file.exists():
        raise ValueError("No dashboards have been designed yet.")

    design = json.loads(config_file.read_text())
    con = duckdb.connect(str(db_path), read_only=True)

    dashboards_out = []
    for d in design.get("dashboards", []):
        kpi_insights = d.get("kpi_insights", {})
        chart_insights = d.get("chart_insights", {})

        kpis_out = []
        for kpi in d.get("kpis", []):
            rows = _execute_query(con, kpi["sql"])
            value = list(rows[0].values())[0] if rows else None
            kpis_out.append({"title": kpi["title"], "format": kpi["format"], "value": value,
                              "insight": kpi_insights.get(kpi["title"], "")})
        kpis_out = [_sanitize_kpi_format(k) for k in kpis_out]

        charts_out = []
        for chart in d.get("charts", []):
            if "sql" not in chart:
                continue
            rows = _execute_query(con, chart["sql"])
            base = {"title": chart["title"], "chart_type": chart["chart_type"],
                    "x_field": chart.get("x_field"), "y_field": chart.get("y_field"),
                    "data": rows, "insight": chart_insights.get(chart["title"], "")}
            if chart["chart_type"] == "heatmap":
                base["value_field"] = chart.get("value_field")
            charts_out.append(_sanitize_chart(base))

        dashboards_out.append({
            "id": d["id"], "title": d["title"], "focus": d.get("focus", ""),
            "kpis": kpis_out, "charts": charts_out, "summary": d.get("summary", ""),
        })

    con.close()
    return {"generated_at": design.get("generated_at"), "dashboards": dashboards_out,
            "rejected_count": 0, "empty_chart_count": 0}

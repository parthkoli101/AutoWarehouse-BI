# Warehouse Console

Single-command ingestion console: upload CSV/Excel files or connect a live
MySQL database, and build a DuckDB warehouse from either — with a live
schema visualization and an AI-designed dashboard on top. Supports
multiple independently named warehouses.

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
cd backend
python app.py
```

Then open **http://localhost:5000**. Everything runs from this one command
— there is no separate `main.py` step anymore; the pipeline (extract →
clean → transform → load) is called in-process from `app.py`.

## Multiple warehouses (topbar)

The topbar has a warehouse selector, "+ New", and "Delete". Every build
button and every panel below operates on **whichever warehouse is
currently selected** — building doesn't touch other warehouses, and
building into a warehouse always **replaces its existing tables first**
(a genuine clean-slate rebuild), rather than adding to whatever a
previous build left there. This matters concretely: if you build once
from Files and then build again from Database into the *same* warehouse,
the Database build fully replaces the Files build's tables — it does not
merge the two sources together. If you want both sources' data available
side by side, create two separate warehouses instead.

Each warehouse is its own file: `data/warehouse/<name>.duckdb`, with its
own dashboard config alongside it (`data/warehouse/<name>_dashboard_config.json`).

## What each panel does

**01 · File Ingestion**
- Choose files, choose a folder, or drag-and-drop CSV/Excel files
- "Stage files" saves them into `data/raw/`
- "Create Warehouse from Files" runs the full pipeline against whatever's
  in `data/raw/` — expects the 9 Olist filenames (see `pipeline/config.py`
  → `SOURCE_TABLES`) in either `.csv` or `.xlsx`/`.xls` form, and builds
  the star schema (`dim_customers`, `dim_products`, `dim_sellers`,
  `dim_date`, `fact_order_items`) into the currently selected warehouse

**02 · Database Connection**
- Enter MySQL credentials (same ones as your MySQL CLI). Use `127.0.0.1`
  rather than `localhost` if your MySQL user isn't explicitly granted
  `@localhost` — see project history for why.
- "Connect" tests the connection and shows the real table list
- "Create Warehouse from Database" then:
  1. Runs `SELECT VERSION()` and stores it — MySQL syntax varies by
     version (window functions/CTEs need 8.0+), so this is kept for any
     future version-aware query generation
  2. Walks real foreign keys via SQLAlchemy's inspector to map how tables
     are actually interlinked (not guessed)
  3. Pulls every table as-is, deduplicates exact-duplicate rows (the only
     safe domain-agnostic cleaning without knowing what the columns mean),
     and loads it into the currently selected warehouse
  4. Writes the version + relationship graph into the warehouse itself, as
     `_warehouse_info` and `_warehouse_relationships` tables

**03 · Warehouse Overview**
- Reads back whatever's currently in the selected warehouse
- Renders an interactive graph (vis.js) of tables as nodes and real FK
  relationships as edges
- Shows a row/column-count summary table alongside it
- "Refresh" re-reads the warehouse (useful after building from either path)

**04 · Dashboard (AI-designed, via local Ollama)**
- "Design Dashboard (AI)" sends the selected warehouse's real schema
  (tables, columns, row counts, sample rows — with ID-like columns
  explicitly flagged as unsuitable for chart labels) to a locally running
  Ollama model (`qwen2.5:7b` by default) and asks it to propose 4 KPIs and
  4 charts as SQL queries against the actual tables
- **Every generated query is validated before it ever touches the
  warehouse**: must be a single `SELECT`, no other statement types, no
  semicolons, blocklist on `DROP`/`DELETE`/`UPDATE`/`INSERT`/`ALTER`/etc.,
  and must reference a real warehouse table. Anything that fails is
  discarded and logged, never executed — tested against a real `DROP
  TABLE` injection attempt during development, which was correctly
  rejected with the table left intact
- **Chart shape is also sanitized server-side, not just prompted for**:
  any chart with more than 10 rows is truncated to the top 10 by value,
  and any `pie` chart with more than 6 slices is force-converted to `bar`
  — regardless of what the model chose. KPI values that look like a
  bounded rating (e.g. a 1–5 review score) mislabeled as `percent` are
  force-corrected to `number` with the scale noted in the title.
- After computing the real numbers, a **second Ollama call** is fed those
  actual values and asked to write a grounded explanation — an overall
  summary plus one insight sentence per KPI and per chart — rather than
  generic text written before any data was seen.
- The design (KPIs + charts + SQL + insights) is saved once per warehouse.
  **"Refresh" is a manual button** that re-runs only the saved queries —
  no repeated LLM calls, so it's fast — and reuses the last design's
  insight text (regenerating narrative on every click would mean another
  30–90s Ollama wait each time).
- Requires Ollama running locally (`ollama serve`) with the model pulled
  (`ollama pull qwen2.5:7b`) only at design time — not needed for refreshes.
  First call after Ollama starts is slow (cold start, loading the model
  into RAM) — run a quick warmup prompt before demoing.
- Charts render via Chart.js with a distinct, vivid color palette
  (deliberately different from the console's muted engineering theme —
  this panel is exec-facing, the rest is technical/utilitarian)

## Honesty note on the two warehouse-build paths

The **Files** path builds a proper star schema, because `config.py` knows
the Olist domain (which table is a fact, which are dimensions).

The **Database** path does NOT build a star schema — it loads tables as-is.
Deciding what's a fact vs. a dimension for an arbitrary, unknown database
is a real classification problem, not yet built (this is the "AI-ETL
fact/dimension classification" piece from earlier project scoping). Until
that exists, the honest thing to do is show the real tables and real
relationships rather than force them into a schema shape that might be
wrong for that domain.

## File structure

```
backend/
  app.py                # Flask app — all routes, runs pipeline in-process
  warehouse_manager.py  # multi-warehouse list/create/delete, clean-slate clearing
  db_connector.py       # MySQL connection, version, FK-walking, table extraction
  ollama_client.py      # local Ollama API client, JSON-only responses
  dashboard_engine.py   # schema→prompt, SQL safety validation, chart/KPI
                         # sanitization, grounded insight generation, persistence
pipeline/
  config.py         # Olist source table registry (paths, PK/FK, dtypes)
  extract.py        # CSV/Excel extraction + validation
  clean.py          # dedup, missing values, IQR outliers (Olist-specific)
  transform.py      # star schema builder (Olist-specific)
  load.py           # DuckDB load + indexing
  quality.py        # quality report generator
  generic_pipeline.py  # MySQL path: generic clean/load + metadata writer
frontend/
  templates/index.html
  static/css/style.css
  static/js/main.js
data/
  raw/          # CSV/Excel files land here
  warehouse/    # <name>.duckdb + <name>_dashboard_config.json live here, one pair per warehouse
```

## Not yet built (next pieces, per project plan)

- Kafka replay simulator
- Schema matcher (embeddings-based column matching)
- ML layer (forecast + anomaly detection)
- NL2SQL (general free-text query, distinct from the dashboard's fixed KPI/chart set)
- MIS report generator

## What's new in this build

**MongoDB integration** — same pattern as MySQL: you start `mongod`
yourself (`mongod` or your MongoDB service), and the backend connects to
`localhost:27017` when `app.py` runs. Python cannot reliably launch a
database server itself, so this isn't literal auto-start — it's
auto-connect on startup, with a clear status shown in the topbar dot next
to "MONGO". If MongoDB isn't running, uploads and builds still work,
falling back to local disk — nothing breaks, you just don't get the
MongoDB-backed storage until it's running.

**File-mixing bug fixed** — each warehouse now gets its own staging
folder (`data/raw/<warehouse>/`) and, when MongoDB is connected, its own
tagged file storage there too. Files uploaded for one warehouse can no
longer end up in another warehouse's build.

**Any dataset, not just Olist** — "Create Warehouse from Files" now
checks whether the uploaded files match Olist's 9 expected filenames. If
they do, it runs the existing star-schema pipeline. If they don't (any
other CSV/Excel dataset), it automatically falls back to a generic
pipeline: every file becomes its own table as-is, and a heuristic
(uniqueness + column-name matching, not ML) infers foreign-key
relationships between tables — same honest approach already used for the
MySQL Database path, now covering file uploads too.

**Warehouse Blueprint** — "Generate Blueprint (AI)" in the Warehouse
Overview panel sends the real schema to Ollama and gets back an overview
paragraph plus a role (fact/dimension/reference/lookup) and one-line
purpose for every table. The graph now color-codes nodes by role. Every
table Ollama describes is checked against the real warehouse — a
hallucinated table name gets silently dropped rather than displayed.

**Bigger, stricter dashboard** — now asks for 6 KPIs and 8 charts instead
of 4+4, explicitly requiring coverage of every table in the schema (not
just the largest one) and rejecting redundant KPIs that restate each
other. The insight/explanation feature from the previous build is
unchanged — every KPI and chart still gets a one-sentence grounded
explanation from a second Ollama pass fed the real computed numbers.

## Multi-dashboard suite (latest)

"Design Dashboards (AI)" no longer produces one flat dashboard — it now:

1. Asks Ollama to identify 2-4 natural **themes** in the actual schema
   (e.g. "Sales", "Customer Support", "Operations" — grounded in real
   tables, never forced if the data doesn't support that many angles)
2. Designs each theme's dashboard with its own focused KPIs/charts (one
   Ollama call per theme)
3. Generates grounded insights for every theme in **one combined call**
   (not one call per chart — keeps total generation time reasonable)
4. Builds a **Centralized Overview** — deterministic, no LLM call: pulls
   the top KPIs/charts from every theme into one combined view, so there's
   always a single "here's everything that matters" dashboard alongside
   the themed deep-dives

Each dashboard appears in the sidebar under Dashboard, selectable
independently. "Refresh" re-runs every query across every dashboard, no
LLM calls.

**New real chart type: heatmap.** When a theme has two genuine categorical
dimensions to cross (e.g. state × category → revenue), Ollama can now
propose a real 2D heatmap — three-column SQL (x, y, value), validated and
field-repaired the same way bar/line/pie already are, rendered via
ECharts. Bar/line/pie charts also switched from Chart.js to ECharts for
better default polish (gradients, smoother animations) and genuine
heatmap/radar support.

Two more real (non-AI) visualizations, built from data already fetched:
- **Table Relationship Heatmap** (Blueprint page) — actual foreign-key
  connections between tables
- **Warehouse Composition Radar** (Dashboard page, Centralized Overview
  only) — real count of fact/dimension/reference tables

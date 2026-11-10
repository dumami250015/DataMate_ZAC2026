from __future__ import annotations
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import date, timedelta
import sqlglot
from sqlglot import exp
from .calendar import resolve
from .util import write_json, read_json


ALLOWED_FUNCTIONS = set("SUM COUNT AVG MIN MAX COALESCE NULLIF ABS ROUND FLOOR CEIL CEILING CAST TRY_CAST DATE_TRUNC DATE_DIFF DATE_ADD EXTRACT STRFTIME STRPTIME TRY_STRPTIME LOWER UPPER TRIM LTRIM RTRIM LENGTH SUBSTRING SUBSTR REPLACE REGEXP_REPLACE REGEXP_MATCHES REGEXP_EXTRACT CONCAT CONCAT_WS SPLIT_PART STRING_SPLIT ARRAY_LENGTH LIST_LENGTH LIST_CONTAINS CONTAINS STARTS_WITH ENDS_WITH ROW_NUMBER RANK DENSE_RANK LAG LEAD FIRST_VALUE LAST_VALUE GREATEST LEAST IF IFNULL ISNULL DATE TIME TIMESTAMP STR_POSITION STRPOS POSITION DAY MONTH YEAR WEEK WEEKDAY QUARTER MOD POWER SQRT SIGN BOOL_AND BOOL_OR STRING_AGG ARRAY_AGG LIST LIST_VALUE UNNEST GENERATE_SERIES RANGE".split())
ALLOWED_FUNCTIONS |= set("DATEDIFF TIMESTAMPDIFF DATEADD TIMESTAMPADD TIMESTAMP_TRUNC TIME_TO_STR STR_TO_TIME GROUP_CONCAT REGEXP_LIKE ARRAY_SIZE ARRAY_CONTAINS SPLIT TS_OR_DS_TO_DATE TS_OR_DS_TO_TIMESTAMP".split())


def validate_sql(sql, allowed_tables):
    statements = sqlglot.parse(sql, read="duckdb")
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError("Only one SELECT/CTE query is allowed")
    tree = statements[0]
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command, exp.Copy, exp.Alter)
    if any(isinstance(node, forbidden) for node in tree.walk()):
        raise ValueError("Mutation/command is forbidden")
    aliases = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    used = set()
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier) or table.db or table.catalog:
            raise ValueError("External/table-function access is forbidden")
        name = table.name
        if name not in aliases:
            if name not in allowed_tables:
                raise ValueError(f"Unknown or forbidden relation {name}")
            used.add(name)
    for function in tree.find_all(exp.Func):
        if isinstance(function, (exp.Case, exp.If, exp.And, exp.Or, exp.Exists)):
            continue
        name = function.name.upper() if isinstance(function, exp.Anonymous) else function.sql_name().upper()
        if name not in ALLOWED_FUNCTIONS:
            raise ValueError(f"Function not allowed: {name}")
    return used


def parameters(plan, question, calendar_path):
    start, end = resolve(plan.time, question.as_of_date, calendar_path)
    cs, ce = resolve(plan.comparison_time, question.as_of_date, calendar_path) if plan.comparison_time else (None, None)
    return {"start": start, "end": end, "as_of": question.as_of_date.isoformat(),
            "as_of_end": (question.as_of_date + timedelta(days=1)).isoformat(),
            "comparison_start": cs, "comparison_end": ce}


def compile_metric(plan):
    source = plan.queries[0].sql.rstrip().rstrip(";")
    base = f"WITH source AS ({source}) "
    if plan.metric == "sum":
        return base + "SELECT COALESCE(SUM(value),0) AS value FROM source"
    if plan.metric == "count":
        return base + "SELECT COUNT(*) AS value FROM source"
    if plan.metric == "distinct_count":
        return base + "SELECT COUNT(DISTINCT entity_key) AS value FROM source"
    if plan.metric == "average":
        # Return exact sum/count; serialization performs Decimal division.
        return base + "SELECT SUM(value) AS total, COUNT(value) AS count FROM source"
    sql = base + "SELECT entity_key, MIN(entity_name) AS entity_name, SUM(value) AS value FROM source GROUP BY entity_key ORDER BY value DESC, entity_key ASC"
    if plan.metric == "rank":
        sql += f" LIMIT {int(plan.limit)}"
    return sql


def run_sql(database, sql, bindings, allowed_tables, config, work, deadline=None):
    used = validate_sql(sql, allowed_tables)
    tree = sqlglot.parse_one(sql, read="duckdb")
    names = {p.name for p in tree.find_all(exp.Placeholder)}
    # DuckDB $name parameters are parsed as Parameter/Var in some sqlglot versions.
    import re
    names |= set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", sql))
    if names - bindings.keys():
        raise ValueError(f"Unknown SQL parameters: {names - bindings.keys()}")
    selected = {n: bindings[n] for n in names}
    if any(v is None for v in selected.values()):
        raise ValueError("SQL uses an unresolved time bound")
    timeout = config.runtime.sql_timeout_seconds
    if deadline:
        deadline.check()
        timeout = min(timeout, deadline.remaining)
    Path(work).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="query_", dir=work) as tmp:
        request = Path(tmp) / "request.json"
        response = Path(tmp) / "response.json"
        write_json(request, {"database": str(Path(database).resolve()), "sql": sql, "bindings": selected,
                             "allowed_tables": sorted(allowed_tables), "response": str(response),
                             "memory_limit": config.runtime.memory_limit, "threads": config.runtime.threads,
                             "max_rows": config.pipeline.max_result_rows})
        completed = subprocess.run([sys.executable, "-m", "datamate.sql_worker", str(request)],
                                   capture_output=True, text=True, timeout=timeout)
        if not response.exists():
            raise RuntimeError("Query worker failed: " + completed.stderr[-2000:])
        result = read_json(response)
        if "error" in result:
            raise ValueError(result["error"])
        result["tables"] = sorted(used)
        return result


def execute_plan(plan, question, context, config, calendar_path, deadline=None):
    allowed = {t["table"] for t in context["catalog"]} | {"solar_lunar_calendar"}
    bindings = parameters(plan, question, calendar_path)
    queries = [compile_metric(plan)] if plan.route == "metric" else [q.sql for q in plan.queries]
    if not any(validate_sql(sql, allowed) for sql in queries):
        raise ValueError("Answer queries must read the supplied business data or calendar; constant-only answers are forbidden")
    results = []
    for sql in queries:
        used = validate_sql(sql, allowed)
        if used - set(plan.tables):
            raise ValueError(f"Query uses undeclared tables: {used - set(plan.tables)}")
        # Block referenced normalized fields with parse failures even if omitted from required_columns.
        columns = {c.name for c in sqlglot.parse_one(sql, read="duckdb").find_all(exp.Column)}
        relevant = [t for t in context["catalog"] if t["table"] in used]
        if bindings["start"] and relevant:
            ends = [t.get("mapping", {}).get("coverage_end") for t in relevant]
            if all(ends) and all(bindings["start"] > end for end in ends):
                raise ValueError("Requested interval is wholly after documented source coverage; zero is not supported")
        if bindings["end"] and relevant:
            starts = [t.get("mapping", {}).get("coverage_start") for t in relevant]
            if all(starts) and all(bindings["end"] <= start for start in starts):
                raise ValueError("Requested interval is wholly before documented source coverage; zero is not supported")
        for table in context["catalog"]:
            if table["table"] in used:
                bad = columns & {n for n, count in table["parse_errors"].items() if count}
                if bad:
                    raise ValueError(f"Query depends on unresolved parsed columns: {sorted(bad)}")
                if "snapshot_quantity" in columns and any(c["name"] == "snapshot_quantity" for c in table.get("columns", [])):
                    names = {c["name"] for c in table["columns"]}
                    if "snapshot_date" not in names:
                        snapshot_day = table.get("mapping", {}).get("coverage_end")
                        requested_day = (date.fromisoformat(bindings["end"]) - timedelta(days=1)).isoformat() if bindings["end"] else bindings["as_of"]
                        if snapshot_day != requested_day:
                            raise ValueError("Undated final snapshot cannot directly answer a different or unknown balance date; use complete movement history")
        result = run_sql(context["database"], sql, bindings, allowed, config, Path(config.paths.work) / "tmp", deadline)
        result["sql"] = sql
        results.append(result)
    checks = []
    for check in plan.checks:
        result = run_sql(context["database"], check.sql, bindings, allowed, config, Path(config.paths.work) / "tmp", deadline)
        if len(result["rows"]) != 1 or len(result["rows"][0]) != 1:
            raise ValueError("Verification check must return one scalar")
        value = result["rows"][0][0]
        ok = value == 0 if check.expectation == "zero" else (value is not None and value > 0) if check.expectation == "positive" else value is True
        checks.append({"description": check.description, "passed": ok, "value": value})
        if not ok:
            raise ValueError(f"Verification failed: {check.description}; got {value}")
    return results, checks, bindings


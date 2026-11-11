from __future__ import annotations
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import date, timedelta
from decimal import Decimal
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope
from .calendar import resolve
from .normalize import parse_date
from .util import write_json, read_json


ALLOWED_FUNCTIONS = set("SUM COUNT AVG MIN MAX COALESCE NULLIF ABS ROUND FLOOR CEIL CEILING CAST TRY_CAST DATE_TRUNC DATE_DIFF DATE_ADD EXTRACT STRFTIME STRPTIME TRY_STRPTIME LOWER UPPER TRIM LTRIM RTRIM LENGTH SUBSTRING SUBSTR REPLACE REGEXP_REPLACE REGEXP_MATCHES REGEXP_EXTRACT CONCAT CONCAT_WS SPLIT_PART STRING_SPLIT ARRAY_LENGTH LIST_LENGTH LIST_CONTAINS CONTAINS STARTS_WITH ENDS_WITH ROW_NUMBER RANK DENSE_RANK LAG LEAD FIRST_VALUE LAST_VALUE GREATEST LEAST IF IFNULL ISNULL DATE TIME TIMESTAMP STR_POSITION STRPOS POSITION DAY MONTH YEAR WEEK WEEKDAY QUARTER MOD POWER SQRT SIGN BOOL_AND BOOL_OR STRING_AGG ARRAY_AGG LIST LIST_VALUE UNNEST GENERATE_SERIES RANGE".split())
ALLOWED_FUNCTIONS |= set("DATEDIFF TIMESTAMPDIFF DATEADD TIMESTAMPADD TIMESTAMP_TRUNC TIME_TO_STR STR_TO_TIME GROUP_CONCAT REGEXP_LIKE ARRAY_SIZE ARRAY_CONTAINS SPLIT TS_OR_DS_TO_DATE TS_OR_DS_TO_TIMESTAMP".split())


def parse_query(sql):
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.SqlglotError as exc:
        raise ValueError(f"Invalid DuckDB SQL: {exc}") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError("Only one SELECT/CTE query is allowed")
    return statements[0]


def validate_sql(sql, allowed_tables):
    tree = parse_query(sql)
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command, exp.Copy, exp.Alter)
    if any(isinstance(node, forbidden) for node in tree.walk()):
        raise ValueError("Mutation/command is forbidden")
    allowed = {name.casefold(): name for name in allowed_tables}
    used = set()
    tables = list(tree.find_all(exp.Table))
    for table in tables:
        if not isinstance(table.this, exp.Identifier) or table.db or table.catalog:
            raise ValueError("External/table-function access is forbidden")
    resolved = set()
    try:
        for scope in traverse_scope(tree):
            for node, source in scope.selected_sources.values():
                if isinstance(node, exp.Table):
                    resolved.add(id(node))
                if isinstance(source, exp.Table):
                    used.add(allowed.get(source.name.casefold(), source.name))
    except sqlglot.errors.SqlglotError as exc:
        raise ValueError(f"Invalid query scope: {exc}") from exc
    if any(id(table) not in resolved for table in tables):
        raise ValueError("Unable to resolve all query relations")
    if used - allowed_tables:
        raise ValueError(f"Unknown or forbidden relations {sorted(used - allowed_tables)}; use catalog tables {sorted(allowed_tables)}")
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
    # Emit explicit AS aliases, including VALUE, which DuckDB 1.3 rejects as a bare alias.
    source = parse_query(plan.queries[0].sql).sql(dialect="duckdb")
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
    return run_sql_batch(database, [sql], bindings, allowed_tables, config, work, deadline)[0]


def run_sql_batch(database, queries, bindings, allowed_tables, config, work, deadline=None):
    used = [validate_sql(sql, allowed_tables) for sql in queries]
    timeout = config.runtime.sql_timeout_seconds
    if deadline:
        deadline.check()
        timeout = min(timeout, deadline.remaining)
    Path(work).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="query_", dir=work) as tmp:
        request = Path(tmp) / "request.json"
        response = Path(tmp) / "response.json"
        write_json(request, {"database": str(Path(database).resolve()), "queries": queries, "bindings": bindings,
                             "allowed_tables": sorted(allowed_tables), "response": str(response),
                             "memory_limit": config.runtime.memory_limit, "threads": config.runtime.threads,
                             "max_rows": config.pipeline.max_result_rows})
        try:
            completed = subprocess.run([sys.executable, "-m", "datamate.sql_worker", str(request)],
                                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                                       timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            if deadline:
                deadline.check()
            raise ValueError(f"Query exceeded its {timeout:g}s execution timeout") from exc
        if not response.exists():
            raise RuntimeError("Query worker failed: " + completed.stderr[-2000:])
        result = read_json(response)
        if "error" in result:
            raise ValueError(result["error"])
        for item, relations in zip(result["results"], used, strict=True):
            item["tables"] = sorted(relations)
        return result["results"]


def documented_coverage(table, columns):
    mapping = table.get("mapping", {})
    def normalized_bound(name):
        try:
            return parse_date(mapping.get(name))
        except ValueError:
            return None

    start, end = normalized_bound("coverage_start"), normalized_bound("coverage_end")
    observed = [bounds for name, bounds in table.get("observed_dates", {}).items() if name in columns]
    # A schema mapping may have been reused across monthly files or sheets.
    # Actual records can refute a claimed bound, but do not prove completeness.
    if start and observed:
        start = min(start, *(bounds[0] for bounds in observed))
    if end and observed:
        end = max(end, *(bounds[1] for bounds in observed))
    return start, end


def execute_plan(plan, question, context, config, calendar_path, deadline=None, *, batch=False):
    allowed = {t["table"] for t in context["catalog"]} | {"solar_lunar_calendar"}
    bindings = parameters(plan, question, calendar_path)
    queries = [compile_metric(plan)] if plan.route == "metric" else [q.sql for q in plan.queries]
    if not any(validate_sql(sql, allowed) for sql in queries):
        raise ValueError("Answer queries must read the supplied business data or calendar; constant-only answers are forbidden")
    for sql in queries:
        used = validate_sql(sql, allowed)
        if used - set(plan.tables):
            raise ValueError(f"Query uses undeclared tables: {used - set(plan.tables)}")
        # Block referenced normalized fields with parse failures even if omitted from required_columns.
        tree = parse_query(sql)
        columns = {c.name.lower() for c in tree.find_all(exp.Column)}
        relevant = [t for t in context["catalog"] if t["table"] in used]
        bounds = {t["table"]: documented_coverage(t, columns) for t in relevant}
        names = {node.name.lower() for node in tree.find_all(exp.Placeholder)}
        windows = []
        if names & {"comparison_start", "comparison_end"}:
            windows.append((bindings["comparison_start"], bindings["comparison_end"]))
        if not windows or names & {"start", "end"}:
            windows.append((bindings["start"], bindings["end"]))
        for window_start, window_end in windows:
            if bounds and window_start and all(end and window_start > end for _, end in bounds.values()):
                raise ValueError(f"Requested interval starting {window_start} is wholly after documented source coverage {bounds}")
            if bounds and window_end and all(start and window_end <= start for start, _ in bounds.values()):
                raise ValueError(f"Requested interval ending before {window_end} is wholly before documented source coverage {bounds}")
        for table in context["catalog"]:
            if table["table"] in used:
                bad = columns & {n for n, count in table["parse_errors"].items() if count}
                if bad:
                    raise ValueError(f"Query depends on unresolved parsed columns: {sorted(bad)}")
                if "snapshot_quantity" in columns and any(c["name"] == "snapshot_quantity" for c in table.get("columns", [])):
                    names = {c["name"] for c in table["columns"]}
                    if "snapshot_date" not in names:
                        _, snapshot_day = documented_coverage(table, ())
                        requested_day = (date.fromisoformat(bindings["end"]) - timedelta(days=1)).isoformat() if bindings["end"] else bindings["as_of"]
                        if snapshot_day != requested_day:
                            raise ValueError("Undated final snapshot cannot directly answer a different or unknown balance date; use complete movement history")
    work = Path(config.paths.work) / "tmp"
    if batch:
        combined = run_sql_batch(context["database"], queries + [check.sql for check in plan.checks],
                                 bindings, allowed, config, work, deadline)
        results, check_results = combined[:len(queries)], combined[len(queries):]
    else:
        results = [run_sql(context["database"], sql, bindings, allowed, config, work, deadline) for sql in queries]
        check_results = (run_sql(context["database"], check.sql, bindings, allowed, config, work, deadline)
                         for check in plan.checks)
    for sql, result in zip(queries, results, strict=True):
        result["sql"] = sql
    checks = []
    for check, result in zip(plan.checks, check_results, strict=True):
        if len(result["rows"]) != 1 or len(result["rows"][0]) != 1:
            raise ValueError("Verification check must return one scalar")
        value = result["rows"][0][0]
        if check.expectation == "true":
            ok = value is True
        else:
            if value is not None and type(value) not in (int, float, Decimal):
                raise ValueError(f"Verification check must return a number for {check.expectation}: {check.description}")
            ok = value is not None and (value == 0 if check.expectation == "zero" else value > 0)
        checks.append({"description": check.description, "passed": ok, "value": value})
        if not ok:
            raise ValueError(f"Verification failed: {check.description}; got {value}")
    return results, checks, bindings


from difflib import SequenceMatcher
from pydantic import ValidationError
from sqlglot import exp
from .calendar import resolve
from .contracts import Plan, Review, TimeSpec
from .execution import documented_coverage, parse_query, validate_sql
from .llm import GenerationError
from .prompts import PLANNER, REVIEW
from .util import norm
from .capabilities import validate_capabilities
from .verified_intent import explicit_question_time


def context_payload(question, context, rules, max_entities=30):
    q = norm(question.question, accents=True)
    entities = sorted(context["entities"], key=lambda e: (-(2 if norm(e["value"], True) in q else 0) - SequenceMatcher(None, q, norm(e["value"], True)).ratio(), e["value"]))[:max_entities]
    catalog = []
    for t in context["catalog"]:
        observed = t.get("observed_dates", {})
        start, end = documented_coverage(t, observed)
        catalog.append({"table": t["table"], "role": t["role"], "grain": t["grain"], "columns": t["columns"],
                        "raw_columns": t["raw_columns"], "row_count": t["row_count"], "parse_errors": t["parse_errors"],
                        "null_counts": t["null_counts"], "samples": t["samples"][:2],
                        "files": sorted({source["file"] for source in t.get("sources", [])}),
                        "distinct_values": t.get("distinct_values", {}),
                        "coverage_start": start, "coverage_end": end, "observed_dates": observed,
                        "notes": t["mapping"].get("notes", [])})
    relative_periods = {kind: resolve(TimeSpec(kind=kind), question.as_of_date)
                        for kind in ("today", "yesterday", "this_week", "previous_week", "this_month", "previous_month")}
    return {"question": question.model_dump(mode="json"), "rules": rules.prompt(), "README": context["readme"],
            "catalog": catalog, "entity_candidates": entities,
            "relative_periods_inclusive_start_exclusive_end": relative_periods,
            "capabilities": context.get("capabilities", {}),
            "lineage_columns": ["_file", "_sheet", "_row", "_parse_errors"],
            "calendar_table": "solar_lunar_calendar"}


def validate_plan(plan, context, rules, sql_fallback=True):
    rules.validate_refs(plan.rule_refs)
    if plan.route in ("sql", "metric"):
        validate_capabilities(plan, context)
    if plan.ambiguity:
        raise ValueError("Unresolved plan ambiguities: " + "; ".join(plan.ambiguity))
    if plan.route == "sql" and not sql_fallback:
        raise ValueError("SQL fallback disabled")
    known = {t["table"]: t for t in context["catalog"]}
    known["solar_lunar_calendar"] = {"columns": [], "raw_columns": {}, "parse_errors": {}}
    if set(plan.tables) - known.keys():
        raise ValueError("Plan references unknown tables")
    for table, columns in plan.required_columns.items():
        if table not in known:
            raise ValueError(f"Unknown required table {table}")
        if table == "solar_lunar_calendar":
            continue
        t = known[table]
        available = {c["name"] for c in t["columns"]} | set(t["raw_columns"])
        if set(columns) - available:
            raise ValueError(f"Unknown required columns in {table}: {sorted(set(columns) - available)}")
        bad = [c for c in columns if t["parse_errors"].get(c, 0)]
        if bad:
            raise ValueError(f"Required fields have unresolved parse failures in {table}: {bad}")
    if plan.route in ("sql", "metric") and not plan.rule_refs:
        raise ValueError("An answer plan must cite applicable rule clauses")


TIME_PARAMETERS = {"start", "end", "comparison_start", "comparison_end", "as_of", "as_of_end"}


def bind_quoted_dates(sql, context):
    """Repair exact placeholder literals in verified temporal expressions only."""
    tree = parse_query(sql)
    by_table = {table["table"].casefold(): table.get("columns", []) for table in context["catalog"]}
    sources = {}
    for table in tree.find_all(exp.Table):
        alias = table.alias_or_name.casefold()
        sources.setdefault(alias, []).append(by_table.get(table.name.casefold(), []))

    def temporal(expression):
        if isinstance(expression, (exp.Cast, exp.TryCast)):
            target = expression.args.get("to")
            return target is not None and target.sql().upper().startswith(("DATE", "TIMESTAMP"))
        if not isinstance(expression, exp.Column):
            return False
        if expression.table:
            candidates = sources.get(expression.table.casefold(), [])
            if len(candidates) != 1:
                return False
            matching = candidates[0]
        else:
            if any(not group for definitions in sources.values() for group in definitions):
                return False
            matching = [c for definitions in sources.values() for group in definitions for c in group]
        types = [c["kind"] for c in matching if c["name"].casefold() == expression.name.casefold()]
        return bool(types) and all(kind in ("date", "datetime") for kind in types)

    changed = False
    for literal in list(tree.find_all(exp.Literal)):
        if not literal.is_string or not literal.this.startswith("$") or literal.this[1:] not in TIME_PARAMETERS:
            continue
        parent = literal.parent
        is_temporal = (
            isinstance(parent, (exp.Cast, exp.TryCast)) and temporal(parent)
            or isinstance(parent, exp.Between) and temporal(parent.this)
            or isinstance(parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE))
            and temporal(parent.expression if parent.this is literal else parent.this)
        )
        if is_temporal:
            literal.replace(exp.Placeholder(this=literal.this[1:]))
            changed = True
    return tree.sql(dialect="duckdb") if changed else sql


def ground_plan(plan, context, question=None):
    """Derive physical relations from SQL. DuckDB binds the actual column references."""
    if plan.route not in ("sql", "metric"):
        return plan
    allowed = {t["table"] for t in context["catalog"]} | {"solar_lunar_calendar"}
    tables = set()
    grounded = {}
    for field in ("queries", "checks"):
        grounded[field] = []
        for query in getattr(plan, field):
            sql = bind_quoted_dates(without_dual(query.sql), context)
            tables.update(validate_sql(sql, allowed))
            grounded[field].append(query.model_copy(update={"sql": sql}))
    if question is not None and plan.comparison_time is None and any(
        any(node.name in ("start", "end") for node in parse_query(q.sql).find_all(exp.Placeholder))
        for q in grounded["queries"]
    ):
        explicit = explicit_question_time(question.question)
        if explicit is not None:
            grounded["time"] = explicit
    return plan.model_copy(update={**grounded, "tables": sorted(tables), "required_columns": {}, "required_capabilities": []})


def without_dual(sql):
    tree = parse_query(sql)
    if any(cte.alias_or_name.casefold() == "dual" for cte in tree.find_all(exp.CTE)):
        return sql
    changed = False
    for select in tree.find_all(exp.Select):
        from_key = "from_" if "from_" in select.args else "from"
        source = select.args.get(from_key)
        table = source.this if source is not None else None
        if (isinstance(table, exp.Table) and table.name.casefold() == "dual" and not table.db
                and not table.catalog and not table.alias and not select.args.get("joins")):
            if not any(True for _ in select.find_all(exp.Column, exp.Star)):
                select.set(from_key, None)
                changed = True
    return tree.sql(dialect="duckdb") if changed else sql


def make_plan(backend, payload, config, deadline=None, prior=None):
    budget = config.model.plan_tokens
    if prior:
        issue = prior["error"]
        instruction = "Fix the reported issue. Emit only the shortest complete plan."
        if "Alias already used" in issue:
            instruction += " Rebuild FROM and JOIN clauses with a different alias for every source in each SELECT."
        elif "Ambiguous reference to column" in issue:
            instruction += " Qualify the ambiguous column with the alias of its intended source in every affected clause."
        elif "does not have a column named" in issue or "Referenced column" in issue:
            instruction += " Check each column against its source table or CTE projection. Use only catalog columns and explicitly projected CTE fields."
        elif "Verification check must return" in issue:
            instruction += " Use COUNT or SUM for numeric checks and a boolean expression for true checks. Return one row and one column."
        elif "Expected one scalar" in issue:
            instruction += " Return exactly one row for each scalar answer. Project only the requested value or specify output.answer_column. Preserve the requested measure and type."
        elif "List query must" in issue:
            instruction += " Return one ordered answer value per row or specify output.answer_column. Use list_scalars for separate scalar queries."
        elif "row cap exceeded" in issue.lower() or "Result exceeded" in issue:
            instruction += " Apply the requested filters, aggregation, or ranking inside SQL before returning the final answer rows."
        payload = dict(payload, repair=dict(prior, instruction=instruction))
        if budget is not None and prior["error"].startswith("GenerationLimitError:"):
            budget = min(budget + 512, config.model.max_new_tokens)
    return backend.structured(PLANNER, payload, Plan,
                              thinking=config.model.repair_thinking if prior else config.model.thinking,
                              deadline=deadline, max_output_tokens=budget,
                              omit_schema_fields=("tables", "required_columns", "required_capabilities"))


def review_plan(backend, payload, plan, results, deadline=None, max_output_tokens=None):
    review_payload = dict(payload, plan=plan.model_dump(mode="json"), result=results)
    try:
        return backend.structured(REVIEW, review_payload, Review, thinking=False,
                                  deadline=deadline, max_output_tokens=max_output_tokens)
    except (GenerationError, ValidationError):
        review_payload["format_retry"] = "Emit only verdict and at most two short issues as JSON."
        return backend.structured(REVIEW, review_payload, Review, thinking=False,
                                  deadline=deadline, max_output_tokens=max_output_tokens)


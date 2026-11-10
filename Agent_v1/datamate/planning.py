from difflib import SequenceMatcher
from .contracts import Plan, Review
from .prompts import PLANNER, REVIEW
from .util import norm
from .capabilities import validate_capabilities


def context_payload(question, context, rules, max_entities=30):
    q = norm(question.question, accents=True)
    entities = sorted(context["entities"], key=lambda e: (-(2 if norm(e["value"], True) in q else 0) - SequenceMatcher(None, q, norm(e["value"], True)).ratio(), e["value"]))[:max_entities]
    catalog = []
    for t in context["catalog"]:
        catalog.append({"table": t["table"], "role": t["role"], "grain": t["grain"], "columns": t["columns"],
                        "raw_columns": t["raw_columns"], "row_count": t["row_count"], "parse_errors": t["parse_errors"],
                        "null_counts": t["null_counts"], "samples": t["samples"][:2],
                        "distinct_values": t.get("distinct_values", {}),
                        "coverage_start": t["mapping"].get("coverage_start"), "coverage_end": t["mapping"].get("coverage_end"),
                        "notes": t["mapping"].get("notes", [])})
    return {"question": question.model_dump(mode="json"), "rules": rules.prompt(), "README": context["readme"],
            "catalog": catalog, "entity_candidates": entities,
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
            raise ValueError(f"Unknown required columns in {table}")
        bad = [c for c in columns if t["parse_errors"].get(c, 0)]
        if bad:
            raise ValueError(f"Required fields have unresolved parse failures in {table}: {bad}")
    if plan.route in ("sql", "metric") and not plan.rule_refs:
        raise ValueError("An answer plan must cite applicable rule clauses")


def make_plan(backend, payload, config, deadline=None, prior=None):
    if prior:
        payload = dict(payload, repair=prior)
    return backend.structured(PLANNER, payload, Plan,
                              thinking=config.model.repair_thinking if prior else config.model.thinking, deadline=deadline)


def review_plan(backend, payload, plan, results, deadline=None):
    return backend.structured(REVIEW, dict(payload, plan=plan.model_dump(mode="json"), result=results), Review, thinking=False, deadline=deadline)


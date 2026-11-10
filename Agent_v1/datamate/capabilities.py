"""Structural capability evidence; absence is never inferred from failed parsing."""


REQUIREMENTS = {
    "line_revenue": [{"line_amount"}],
    "order_count": [{"order_id"}],
    "base_quantity": [{"quantity_base"}, {"quantity_selling", "selling_unit", "conversion_factor"}],
    "gross_profit": [{"line_amount", "quantity_selling", "unit_cost_selling"}],
    "historical_debt": [{"order_id", "total_payable", "payment_amount", "paid_at"}],
    "stock_snapshot": [{"snapshot_quantity"}],
    "stock_ledger": [{"stock_balance", "movement_at"}],
    "lot_expiry": [{"lot_id", "expiry_date"}],
}


def catalog_capabilities(catalog):
    fields = {}
    for table in catalog:
        for column in table["columns"]:
            name = column["name"]
            available = table["row_count"] > table["null_counts"].get(name, 0) and not table["parse_errors"].get(name)
            fields.setdefault(name, []).append({"table": table["table"], "usable": available})
    result = {}
    for capability, alternatives in REQUIREMENTS.items():
        chosen = next((group for group in alternatives if all(any(f["usable"] for f in fields.get(name, [])) for name in group)), None)
        result[capability] = {
            "state": "supported" if chosen else "unknown",
            "scope": "structural prerequisites only; join, status, time and completeness must still be verified",
            "evidence": {name: fields[name] for name in sorted(chosen or set())},
            "missing_alternatives": [sorted(group - fields.keys()) for group in alternatives],
        }
    return result


def validate_capabilities(plan, context):
    for name in plan.required_capabilities:
        capability = context.get("capabilities", {}).get(name)
        if capability is None:
            raise ValueError(f"Unknown capability {name}; use source evidence with a SQL plan instead")
        if capability["state"] != "supported":
            raise ValueError(f"Capability {name} is unresolved, not proven absent: {capability}")


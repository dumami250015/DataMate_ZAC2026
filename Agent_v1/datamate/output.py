from __future__ import annotations
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from .util import dumps, read_json, atomic_text, write_json


def decimal(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"Expected numeric value, got {value!r}")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Nonfinite result")
    return result


def rounded(value, places=None):
    d = decimal(value)
    if places is not None:
        d = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return int(d) if d == d.to_integral_value() else d


def scalar(result):
    if len(result["rows"]) != 1 or len(result["rows"][0]) != 1:
        raise ValueError("Expected one scalar result")
    v = result["rows"][0][0]
    if v is None:
        raise ValueError("NULL is not a supported answer")
    return v


def serialize(question_id, plan, results):
    kind, places = plan.output.type, plan.output.decimals
    if plan.postprocess in ("ratio_percent", "growth_percent", "difference", "average_money"):
        a, b = (decimal(scalar(r)) for r in results)
        if plan.postprocess == "difference":
            answer = a - b
        else:
            if b == 0:
                raise ValueError("Undefined zero denominator")
            answer = (a - b) / b * 100 if plan.postprocess == "growth_percent" else a / b * (100 if plan.postprocess == "ratio_percent" else 1)
    elif plan.postprocess == "list_scalars":
        answer = [scalar(r) for r in results]
    elif plan.route == "metric" and plan.metric == "average":
        total, count = results[0]["rows"][0]
        if not count:
            raise ValueError("Average of empty population")
        answer = decimal(total) / decimal(count)
    elif plan.route == "metric" and plan.metric in ("rank", "group_sum"):
        rows = results[0]["rows"]
        if kind == "string":
            if len(rows) != 1:
                raise ValueError("Single winner missing or non-unique output")
            answer = rows[0][1]
        elif kind == "list":
            answer = [r[1] for r in rows]
        elif kind == "table":
            answer = {"columns": plan.output.columns or ["name", "value"], "rows": [[r[1], r[2]] for r in rows]}
        else:
            raise ValueError("Ranking requires string/list/table output")
    else:
        if len(results) != 1:
            raise ValueError("Direct output requires one query")
        result = results[0]
        if kind == "table":
            answer = {"columns": plan.output.columns or result["columns"], "rows": result["rows"]}
        elif kind == "list":
            if any(len(row) != 1 for row in result["rows"]):
                raise ValueError("List query must return one ordered value per row")
            answer = [row[0] for row in result["rows"]]
        else:
            answer = scalar(result)
    if kind in ("number", "percent"):
        answer = rounded(answer, 2 if kind == "percent" and places is None else places)
    elif kind == "list":
        if plan.output.item_decimals and len(plan.output.item_decimals) != len(answer):
            raise ValueError("item_decimals length must match list")
        answer = [rounded(v, plan.output.item_decimals[i] if plan.output.item_decimals else places) if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) else v for i, v in enumerate(answer)]
    elif kind == "table":
        if plan.output.column_decimals and len(plan.output.column_decimals) != len(answer["columns"]):
            raise ValueError("column_decimals length must match columns")
        answer["rows"] = [[rounded(v, plan.output.column_decimals[i] if plan.output.column_decimals else places) if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) else v for i, v in enumerate(row)] for row in answer["rows"]]
    record = {"id": question_id, "type": kind, "answer": answer}
    validate_record(record)
    return record


def validate_record(record):
    kind = record.get("type")
    if not isinstance(record.get("id"), str):
        raise ValueError("Missing string id")
    if kind in ("abstain", "unanswerable"):
        if "reason" in record and not isinstance(record["reason"], str):
            raise ValueError("reason must be a string")
        return
    value = record.get("answer")
    if kind in ("number", "percent"):
        decimal(value)
    elif kind == "string":
        if not isinstance(value, str) or not value:
            raise ValueError("String answer must be nonempty")
    elif kind == "boolean":
        if type(value) is not bool:
            raise ValueError("Boolean must be true/false")
    elif kind == "date":
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", value):
            raise ValueError("Date must be ISO day or month")
        date.fromisoformat(value if len(value) == 10 else value + "-01")
    elif kind == "list":
        if not isinstance(value, list) or any(v is None or isinstance(v, (list, dict)) for v in value):
            raise ValueError("List must contain non-null scalar values")
    elif kind == "table":
        if not isinstance(value, dict) or set(value) != {"columns", "rows"}:
            raise ValueError("Invalid table shape")
        cols = value["columns"]
        if not isinstance(cols, list) or not cols or len(set(cols)) != len(cols) or not all(isinstance(c, str) for c in cols):
            raise ValueError("Invalid table columns")
        if not isinstance(value["rows"], list) or any(not isinstance(row, list) or len(row) != len(cols) or any(v is None or isinstance(v, (list, dict)) for v in row) for row in value["rows"]):
            raise ValueError("Invalid table rows")
    else:
        raise ValueError(f"Unknown output type {kind}")
    dumps(record)


class Journal:
    def __init__(self, root, fingerprint, config):
        self.root = Path(root) / "runs" / fingerprint
        self.root.mkdir(parents=True, exist_ok=True)
        write_json(self.root / "config.json", config)
        self.count = 0

    def has(self, qid):
        p = self.root / "results" / (qid + ".json")
        if not p.exists():
            return False
        try:
            record = read_json(p)
            validate_record(record)
            return record["id"] == qid
        except (ValueError, OSError):
            return False

    def commit(self, qid, record, log):
        validate_record(record)
        write_json(self.root / "logs" / (qid + ".json"), log)
        write_json(self.root.parent.parent / "logs" / (qid + ".json"), log)
        write_json(self.root / "results" / (qid + ".json"), record)
        self.count += 1

    def export(self, questions, public_path=None, publish_logs=False):
        records = [read_json(self.root / "results" / (q.id + ".json")) if self.has(q.id) else {"id": q.id, "type": "abstain"} for q in questions]
        text = "".join(dumps(r) + "\n" for r in records)
        atomic_text(self.root / "submission.jsonl", text)
        if public_path:
            atomic_text(public_path, text)
        if publish_logs:
            for q, record in zip(questions, records):
                source = self.root / "logs" / (q.id + ".json")
                log = read_json(source) if source.exists() else {"id": q.id, "final_answer": record, "elapsed_seconds": 0, "status": "not_completed"}
                write_json(self.root.parent.parent / "logs" / (q.id + ".json"), log)
        write_json(self.root / "progress.json", {"total": len(questions), "completed": sum(self.has(q.id) for q in questions)})
        return self.root / "submission.jsonl"


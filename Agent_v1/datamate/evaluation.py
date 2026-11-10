"""Local scorer. Floating tolerance is an explicit approximation of organizer behavior."""
import random
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from .util import loads, norm, write_json
from .output import validate_record


def read_jsonl(path):
    out = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            record = loads(line)
            if record["id"] in out:
                raise ValueError(f"Duplicate ID {record['id']}")
            out[record["id"]] = record
    return out


def equal(a, b, aliases=()):
    if isinstance(b, bool):
        return type(a) is bool and a == b
    if isinstance(b, (int, float, Decimal)):
        if isinstance(a, bool) or not isinstance(a, (int, float, Decimal)):
            return False
        # Absolute epsilon only: relative tolerance would accept whole-VND mistakes on large totals.
        return abs(Decimal(str(a)) - Decimal(str(b))) <= Decimal("0.000001")
    if isinstance(b, str):
        return isinstance(a, str) and norm(a) in {norm(x) for x in [b, *aliases] if isinstance(x, str)}
    if isinstance(b, list):
        return isinstance(a, list) and len(a) == len(b) and all(equal(x, y, aliases[i] if i < len(aliases) and isinstance(aliases[i], list) else ()) for i, (x, y) in enumerate(zip(a, b)))
    return a == b


def table_equal(a, b, aliases=()):
    if not isinstance(a, dict) or set(a) != {"columns", "rows"}:
        return False
    if len(a["columns"]) != len(b["columns"]):
        return False
    ac, bc = [norm(x) for x in a["columns"]], [norm(x) for x in b["columns"]]
    order = [ac.index(x) for x in bc] if len(set(ac)) == len(ac) and set(ac) == set(bc) else list(range(len(bc)))
    actual = [[row[i] for i in order] for row in a["rows"]]
    if len(actual) != len(b["rows"]):
        return False
    # Bipartite matching avoids a greedy match consuming the only valid row for another reference.
    matches = {}
    def assign(i, visited):
        for j, row in enumerate(actual):
            row_aliases = [aliases[i]] if i < len(aliases) and isinstance(aliases[i], list) else []
            if j not in visited and equal(row, b["rows"][i], row_aliases):
                visited.add(j)
                if j not in matches or assign(matches[j], visited):
                    matches[j] = i
                    return True
        return False
    return all(assign(i, set()) for i in range(len(b["rows"])))


def score_one(prediction, reference):
    if prediction is None or prediction.get("type") == "abstain":
        return 0
    try:
        validate_record(prediction)
    except (ValueError, TypeError):
        return -1
    if prediction["type"] != reference["type"]:
        return -1
    kind = reference["type"]
    if kind == "unanswerable":
        return 1
    if kind == "date":
        ok = prediction["answer"] == reference["answer"]
    elif kind == "table":
        ok = table_equal(prediction["answer"], reference["answer"], reference.get("alias", []))
    else:
        aliases = reference.get("alias", []) if kind in ("string", "list") else []
        ok = equal(prediction["answer"], reference["answer"], aliases)
    return 1 if ok else -1


def summarize(scores):
    counts = Counter(scores)
    n = len(scores)
    attempted = counts[1] + counts[-1]
    return {"n": n, "correct": counts[1], "wrong": counts[-1], "abstain": counts[0],
            "pa": sum(scores) / n if n else 0, "coverage": attempted / n if n else 0,
            "attempted_accuracy": counts[1] / attempted if attempted else 0}


def evaluate(predictions, answers, questions=None, output=None, selected_only=False):
    pred, refs = read_jsonl(predictions), read_jsonl(answers)
    qs = read_jsonl(questions) if questions else {}
    if set(pred) - refs.keys():
        raise ValueError("Predictions contain IDs absent from references")
    rows, types, businesses = [], defaultdict(list), defaultdict(list)
    for qid, ref in refs.items():
        if selected_only and qid not in pred:
            continue
        s = score_one(pred.get(qid), ref)
        business = qs.get(qid, {}).get("business_id", "unknown")
        rows.append({"id": qid, "score": s, "reference_type": ref["type"], "business_id": business})
        types[ref["type"]].append(s); businesses[business].append(s)
    report = summarize([r["score"] for r in rows])
    report["by_type"] = {k: summarize(v) for k, v in types.items()}
    report["by_business"] = {k: summarize(v) for k, v in businesses.items()}
    tp = sum(pred.get(r["id"], {}).get("type") == "unanswerable" and r["score"] == 1 for r in rows)
    attempted_u = sum(pred.get(r["id"], {}).get("type") == "unanswerable" for r in rows)
    actual_u = sum(r["reference_type"] == "unanswerable" for r in rows)
    report["unanswerable_precision"] = tp / attempted_u if attempted_u else None
    report["unanswerable_recall"] = tp / actual_u if actual_u else None
    if output:
        write_json(output, report)
        write_json(Path(output).with_name("question_scores.json"), rows)
    return report


def business_folds(questions_path, folds=5, seed=42):
    qs = read_jsonl(questions_path)
    businesses = sorted({q["business_id"] for q in qs.values()})
    random.Random(seed).shuffle(businesses)
    return [{"fold": i, "validation": businesses[i::folds], "training": [b for j, b in enumerate(businesses) if j % folds != i]} for i in range(folds)]


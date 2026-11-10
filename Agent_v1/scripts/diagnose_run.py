"""Summarize actual abstention reasons without loading a model or training labels."""
import argparse
import json
from collections import Counter
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("run", help="Path to runs/<fingerprint>")
args = parser.parse_args()
root = Path(args.run)
errors = Counter()
examples = {}
for path in sorted((root / "logs").glob("*.json")):
    log = json.loads(path.read_text(encoding="utf-8"))
    reasons = []
    for attempt in log.get("attempts", []):
        if attempt.get("error"):
            reasons.append(attempt["error"])
        elif attempt.get("decision"):
            reasons.append(attempt["decision"])
        elif attempt.get("review", {}).get("verdict") != "accept" and attempt.get("review"):
            reasons.append(str(attempt["review"]))
    for field in ("preparation_error", "unexpected_error"):
        if log.get(field):
            reasons.append(log[field])
    if log.get("final_answer", {}).get("type") == "abstain" and not reasons:
        reasons.append("No recorded reason")
    for reason in reasons:
        category = reason.split(":", 1)[0]
        errors[category] += 1
        examples.setdefault(category, {"file": str(path), "reason": reason})
print(json.dumps({"counts": errors, "examples": examples}, ensure_ascii=False, indent=2))

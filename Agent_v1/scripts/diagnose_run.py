"""Summarize actual abstention reasons without loading a model or training labels."""
import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import median

parser = argparse.ArgumentParser()
parser.add_argument("run", help="Path to runs/<fingerprint>")
args = parser.parse_args()
root = Path(args.run)
selection_path = root / "selection.json"
selected = set(json.loads(selection_path.read_text(encoding="utf-8"))["ids"]) if selection_path.is_file() else None
errors = Counter()
outcomes = Counter()
strategies = Counter()
gaps = Counter()
examples = {}
failed_attempts = Counter()
model_attempts = Counter()
elapsed = []
committed = set()
for path in sorted((root / "logs").glob("*.json")):
    if selected is not None and path.stem not in selected:
        continue
    log = json.loads(path.read_text(encoding="utf-8"))
    # The journal writes the log before committing the result.
    if selected is not None and not (root / "results" / path.name).is_file():
        continue
    if log.get("status") == "not_completed":
        continue
    committed.add(path.stem)
    outcome = log.get("final_answer", {}).get("type", "unknown")
    outcomes[outcome] += 1
    attempts = log.get("attempts", [])
    for entry in attempts:
        if entry.get("error"):
            failed_attempts[entry.get("stage", "unknown")] += 1
    model_attempts[str(sum(entry.get("strategy") == "model_plan" for entry in attempts))] += 1
    if isinstance(seconds := log.get("elapsed_seconds"), (int, float)):
        elapsed.append(seconds)
    attempt = attempts[-1] if attempts else {}
    if outcome != "abstain":
        strategies[attempt.get("strategy", "unknown")] += 1
        continue
    if gap := log.get("verified_gap"):
        gaps[gap["code"]] += 1
    if log.get("preparation_error"):
        category, reason = "preparation", log["preparation_error"]
    elif log.get("unexpected_error"):
        category, reason = "unexpected", log["unexpected_error"]
    elif not attempt and gap:
        category, reason = "verified_only", gap["detail"]
    else:
        category = attempt.get("stage", log.get("status", "unknown"))
        reason = attempt.get("error") or attempt.get("review") or attempt.get("decision") or "No recorded reason"
    errors[category] += 1
    examples.setdefault(category, {"file": str(path), "reason": reason})
progress_path = root / "progress.json"
progress = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.is_file() else None
if selected is not None:
    progress = {"total": len(selected), "completed": len(committed), "unfinished": len(selected - committed)}
print(json.dumps({"progress": progress, "outcomes": outcomes, "answers_by_strategy": strategies, "abstentions_by_stage": errors,
                  "failed_attempts_by_stage": failed_attempts, "questions_by_model_attempt_count": model_attempts,
                  "question_seconds": {"count": len(elapsed), "median": round(median(elapsed), 2) if elapsed else None,
                                       "total": round(sum(elapsed), 2)},
                  "verified_gaps_on_abstentions": gaps, "examples": examples}, ensure_ascii=False, indent=2))

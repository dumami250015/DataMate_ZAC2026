from __future__ import annotations
import csv
import logging
import time
import traceback
from pathlib import Path
import duckdb
from .contracts import Question
from .execution import execute_plan
from .llm import load_backend
from .output import Journal, serialize
from .planning import context_payload, make_plan, review_plan, validate_plan
from .readers import manifest
from .rules import Rulebook
from .runtime import Deadline, checkpoint_fingerprint, environment, source_fingerprint
from .storage import build_business
from .util import digest, file_hash, loads, write_json

logger = logging.getLogger("datamate")


def read_questions(split, businesses=None, limit=None):
    questions, ids = [], set()
    for line in (Path(split) / "questions.jsonl").read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        q = Question.model_validate(loads(line))
        if q.id in ids:
            raise ValueError(f"Duplicate question id {q.id}")
        ids.add(q.id)
        if not businesses or q.business_id in businesses:
            questions.append(q)
    return questions if limit is None else questions[:limit]


def add_calendar(database, calendar_path):
    con = duckdb.connect(database)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS solar_lunar_calendar(date DATE, weekday INTEGER, lunar_day INTEGER, lunar_month INTEGER, lunar_year INTEGER, lunar_leap BOOLEAN, holiday VARCHAR)")
        if con.execute("SELECT COUNT(*) FROM solar_lunar_calendar").fetchone()[0] == 0:
            with open(calendar_path, encoding="utf-8-sig", newline="") as f:
                rows = [(r["date"], int(r["weekday"]), int(r["lunar_day"]), int(r["lunar_month"]), int(r["lunar_year"]), r["lunar_leap"].lower() in ("true", "1"), r.get("holiday", "")) for r in csv.DictReader(f)]
            con.executemany("INSERT INTO solar_lunar_calendar VALUES (?,?,?,?,?,?,?)", rows)
    finally:
        con.close()


class Agent:
    def __init__(self, config, backend=None, raw_only=False):
        self.config = config
        self.deadline = Deadline(config.runtime.run_deadline_seconds, config.runtime.finalization_reserve_seconds)
        config.validate_paths(require_model=backend is None and not raw_only)
        self.rules = Rulebook(Path(config.paths.split) / "rulebook.md")
        self.calendar_path = Path(config.paths.split) / "calendar.csv"
        self.environment = environment(config.paths.work)
        self.code_hash = source_fingerprint()
        self.model_hash = checkpoint_fingerprint(config.paths.model) if backend is None and not raw_only else getattr(backend, "identity", "raw-only")
        self.backend = backend if backend is not None else None if raw_only else load_backend(config.paths.model, config.model, config.runtime.seed, self.model_hash)
        self.cache_root = Path(config.paths.work) / "cache" / digest({"code": self.code_hash, "calendar": file_hash(self.calendar_path)})
        if self.backend:
            self.backend.smoke_test(self.deadline)
        write_json(Path(config.paths.work) / "environment.json", self.environment)
        write_json(self.cache_root / "rules.json", {"hash": self.rules.hash, "clauses": self.rules.clauses})

    def prepare(self, business_id):
        self.deadline.check()
        context = build_business(Path(self.config.paths.split) / "data" / business_id, self.cache_root,
                                 self.rules, self.backend if self.config.pipeline.mapping_with_llm else None,
                                 self.config.pipeline.profile_samples, self.deadline)
        add_calendar(context["database"], self.calendar_path)
        return context

    def answer(self, question, context):
        started = time.monotonic()
        log = {"id": question.id, "question": question.model_dump(mode="json"), "attempts": [], "business_fingerprint": context["fingerprint"]}
        record = {"id": question.id, "type": "abstain"}
        payload = context_payload(question, context, self.rules, self.config.pipeline.max_entity_candidates)
        prior = None
        for attempt in range(self.config.pipeline.semantic_repairs + 1):
            plan = None
            try:
                self.deadline.check()
                plan = make_plan(self.backend, payload, self.config, self.deadline, prior)
                validate_plan(plan, context, self.rules, self.config.pipeline.generated_sql_fallback)
                info = {"plan": plan.model_dump(mode="json")}
                if plan.route == "abstain":
                    info["decision"] = "planner_abstain"
                    log["attempts"].append(info)
                    break
                if plan.route == "unanswerable":
                    if not self.config.pipeline.verify_with_llm:
                        raise ValueError("Unanswerable requires independent review")
                    review = review_plan(self.backend, payload, plan, {"absence_evidence": plan.evidence}, self.deadline)
                    info["review"] = review.model_dump()
                    if review.verdict != "accept":
                        raise ValueError("Absence review failed: " + "; ".join(review.issues))
                    record = {"id": question.id, "type": "unanswerable", "reason": plan.reason}
                else:
                    results, checks, bindings = execute_plan(plan, question, context, self.config, self.calendar_path, self.deadline)
                    info.update({"results": results, "checks": checks, "bindings": bindings})
                    record = serialize(question.id, plan, results)
                    if self.config.pipeline.verify_with_llm:
                        preview = [{"columns": r["columns"], "rows": r["rows"][:20], "total_rows": len(r["rows"])} for r in results]
                        review = review_plan(self.backend, payload, plan, preview, self.deadline)
                        info["review"] = review.model_dump()
                        if review.verdict != "accept":
                            record = {"id": question.id, "type": "abstain"}
                            if review.verdict == "abstain":
                                log["attempts"].append(info)
                                break
                            raise ValueError("Semantic review requests repair: " + "; ".join(review.issues))
                log["attempts"].append(info)
                break
            except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
                record = {"id": question.id, "type": "abstain"}
                prior = {"error": f"{type(exc).__name__}: {exc}", "previous_plan": plan.model_dump(mode="json") if plan else None}
                log["attempts"].append(prior)
                logger.warning("%s attempt %d failed: %s", question.id, attempt + 1, prior["error"])
                if isinstance(exc, TimeoutError):
                    break
        log.update({"final_answer": record, "elapsed_seconds": time.monotonic() - started,
                    "model_stats": getattr(self.backend, "last_stats", {})})
        return record, log

    def predict(self, businesses=None, limit=None):
        if self.backend is None:
            raise ValueError("Prediction requires a model backend")
        questions = read_questions(self.config.paths.split, businesses, limit)
        if not questions:
            raise ValueError("No selected questions")
        root = Path(self.config.paths.split)
        data_manifest = {b: manifest(root / "data" / b) for b in sorted({q.business_id for q in questions})}
        fingerprint = digest({"config": self.config.model_dump(), "code": self.code_hash, "model": self.model_hash,
                              "backend": self.backend.identity, "rules": self.rules.hash,
                              "calendar": file_hash(self.calendar_path), "questions": file_hash(root / "questions.jsonl"),
                              "data": data_manifest, "environment": {k: v for k, v in self.environment.items() if k != "free_disk"}})
        if not self.config.runtime.resume:
            fingerprint += "_" + str(time.time_ns())
        journal = Journal(self.config.paths.work, fingerprint, self.config.model_dump())
        write_json(journal.root / "manifest.json", {"fingerprint": fingerprint, "model": self.model_hash, "code": self.code_hash, "data": data_manifest, "environment": self.environment})
        output = Path(self.config.paths.work) / "submission.jsonl"
        journal.export(questions, output)
        grouped = {}
        for q in questions:
            grouped.setdefault(q.business_id, []).append(q)
        try:
            for business, batch in grouped.items():
                self.deadline.check()
                if all(journal.has(q.id) for q in batch):
                    continue
                logger.info("Preparing %s (%d questions)", business, len(batch))
                try:
                    context = self.prepare(business)
                except TimeoutError:
                    raise
                except Exception as exc:
                    logger.exception("Business preparation failed: %s", business)
                    if self.config.runtime.fail_fast:
                        raise
                    for q in batch:
                        if not journal.has(q.id):
                            record = {"id": q.id, "type": "abstain"}
                            journal.commit(q.id, record, {"id": q.id, "final_answer": record, "elapsed_seconds": 0,
                                                        "preparation_error": f"{type(exc).__name__}: {exc}"})
                    journal.export(questions, output)
                    continue
                for q in batch:
                    self.deadline.check()
                    if journal.has(q.id):
                        continue
                    try:
                        record, log = self.answer(q, context)
                        self.deadline.check()  # timed-out attempts remain resumable, not completed abstentions
                    except TimeoutError:
                        raise
                    except Exception as exc:
                        if self.config.runtime.fail_fast:
                            raise
                        logger.exception("Unexpected question failure %s", q.id)
                        record = {"id": q.id, "type": "abstain"}
                        log = {"id": q.id, "final_answer": record, "elapsed_seconds": 0, "unexpected_error": traceback.format_exc()}
                    journal.commit(q.id, record, log)
                    logger.info("%s -> %s, %.1fs", q.id, record["type"], log["elapsed_seconds"])
                    if journal.count % self.config.runtime.export_every_questions == 0:
                        journal.export(questions, output)
        except TimeoutError:
            logger.warning("Deadline reached; exporting completed answers and abstentions")
        finally:
            journal.export(questions, output, publish_logs=True)
        return journal.root


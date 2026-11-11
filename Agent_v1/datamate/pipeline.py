from __future__ import annotations
import csv
import logging
import time
import traceback
from pathlib import Path
import duckdb
from pydantic import ValidationError
from .contracts import Question
from .execution import execute_plan
from .llm import BackendMemoryError, load_backend
from .output import Journal, serialize
from .planning import context_payload, ground_plan, make_plan, review_plan, validate_plan
from .readers import manifest
from .rules import Rulebook
from .runtime import Deadline, checkpoint_fingerprint, environment, source_fingerprint
from .storage import build_business
from .util import digest, file_hash, loads, write_json
from .verified import assess_verified_query

logger = logging.getLogger("datamate")


def read_questions(split, businesses=None, limit=None, *, offset=0):
    if offset < 0 or (limit is not None and limit <= 0):
        raise ValueError("Question offset must be nonnegative and limit must be positive")
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
    end = None if limit is None else offset + limit
    return questions[offset:end]


def add_calendar(database, calendar_path):
    con = duckdb.connect(database)
    try:
        con.execute("BEGIN TRANSACTION")
        con.execute("CREATE TABLE IF NOT EXISTS solar_lunar_calendar(date DATE, weekday INTEGER, lunar_day INTEGER, lunar_month INTEGER, lunar_year INTEGER, lunar_leap BOOLEAN, holiday VARCHAR)")
        if con.execute("SELECT COUNT(*) FROM solar_lunar_calendar").fetchone()[0] == 0:
            with open(calendar_path, encoding="utf-8-sig", newline="") as f:
                rows = [(r["date"], int(r["weekday"]), int(r["lunar_day"]), int(r["lunar_month"]), int(r["lunar_year"]), r["lunar_leap"].lower() in ("true", "1"), r.get("holiday", "")) for r in csv.DictReader(f)]
            con.executemany("INSERT INTO solar_lunar_calendar VALUES (?,?,?,?,?,?,?)", rows)
        con.execute("COMMIT")
    finally:
        con.close()


class Agent:
    def __init__(self, config, backend=None, raw_only=False, *, verified_only=False):
        self.config = config
        self.deadline = Deadline(config.runtime.run_deadline_seconds, config.runtime.finalization_reserve_seconds)
        if verified_only and (raw_only or backend is not None):
            raise ValueError("Verified-only mode uses the documented schemas without a model")
        self.verified_only = verified_only
        needs_checkpoint = backend is None and not (raw_only or verified_only)
        config.validate_paths(require_model=needs_checkpoint)
        self.rules = Rulebook(Path(config.paths.split) / "rulebook.md")
        self.calendar_path = Path(config.paths.split) / "calendar.csv"
        self.environment = environment(config.paths.work)
        self.code_hash = source_fingerprint()
        self.model_hash = checkpoint_fingerprint(config.paths.model) if needs_checkpoint else getattr(
            backend, "identity", "verified-only" if verified_only else "raw-only",
        )
        self.backend = load_backend(config.paths.model, config.model, config.runtime.seed, self.model_hash) if needs_checkpoint else backend
        self.cache_root = Path(config.paths.work) / "cache" / digest({"code": self.code_hash, "calendar": file_hash(self.calendar_path)})
        if self.backend:
            self.backend.smoke_test(self.deadline)
        write_json(Path(config.paths.work) / "environment.json", self.environment)
        write_json(self.cache_root / "rules.json", {"hash": self.rules.hash, "clauses": self.rules.clauses})

    def prepare(self, business_id):
        self.deadline.check()
        context = build_business(Path(self.config.paths.split) / "data" / business_id, self.cache_root,
                                 self.rules, self.backend if self.config.pipeline.mapping_with_llm else None,
                                 self.config.pipeline.profile_samples, self.deadline,
                                 use_documentation=self.verified_only)
        add_calendar(context["database"], self.calendar_path)
        return context

    def answer(self, question, context):
        started = time.monotonic()
        log = {"id": question.id, "question": question.model_dump(mode="json"), "attempts": [], "business_fingerprint": context["fingerprint"]}
        record = {"id": question.id, "type": "abstain"}
        verified, gap = assess_verified_query(question, context, self.rules)
        if gap:
            log["verified_gap"] = gap
        if verified is not None:
            info = {"stage": "execution", "strategy": "verified_query", "plan": verified.model_dump(mode="json")}
            log["attempts"].append(info)
            try:
                self.deadline.check()
                validate_plan(verified, context, self.rules)
                results, checks, bindings = execute_plan(
                    verified, question, context, self.config, self.calendar_path, self.deadline, batch=True,
                )
                record = serialize(question.id, verified, results)
            except ValueError as exc:
                info["error"] = str(exc)
                log["verified_gap"] = {"code": "execution_rejected", "detail": str(exc)}
                logger.warning("%s verified query fell back to planning: %s", question.id, exc)
            else:
                info.update({"stage": "complete", "results": results, "checks": checks, "bindings": bindings})
                log.update({"final_answer": record, "elapsed_seconds": time.monotonic() - started, "model_stats": {}})
                return record, log
        if self.verified_only:
            log.update({"final_answer": record, "elapsed_seconds": time.monotonic() - started, "model_stats": {}})
            return record, log
        payload = context_payload(question, context, self.rules, self.config.pipeline.max_entity_candidates)
        prior = None
        for attempt in range(self.config.pipeline.semantic_repairs + 1):
            plan = None
            info = {"stage": "planning", "strategy": "model_plan"}
            log["attempts"].append(info)
            try:
                self.deadline.check()
                plan = make_plan(self.backend, payload, self.config, self.deadline, prior)
                info["plan"] = plan.model_dump(mode="json")
                if plan.route == "abstain":
                    info["decision"] = "planner_abstain"
                    raise ValueError(plan.reason or "; ".join(plan.ambiguity) or "Planner declined without evidence")
                info["stage"] = "validation"
                plan = ground_plan(plan, context, question)
                info["plan"] = plan.model_dump(mode="json")
                validate_plan(plan, context, self.rules, self.config.pipeline.generated_sql_fallback)
                if plan.route == "unanswerable":
                    if not self.config.pipeline.verify_with_llm:
                        raise ValueError("Unanswerable requires independent review")
                    info["stage"] = "review"
                    review = review_plan(self.backend, payload, plan, {"absence_evidence": plan.evidence},
                                         self.deadline, self.config.model.plan_tokens)
                    info["review"] = review.model_dump()
                    if review.verdict != "accept":
                        raise ValueError("Absence review failed: " + "; ".join(review.issues))
                    record = {"id": question.id, "type": "unanswerable", "reason": plan.reason}
                else:
                    info["stage"] = "execution"
                    results, checks, bindings = execute_plan(plan, question, context, self.config, self.calendar_path, self.deadline)
                    info.update({"results": results, "checks": checks, "bindings": bindings})
                    info["stage"] = "serialization"
                    record = serialize(question.id, plan, results)
                    if self.config.pipeline.verify_with_llm:
                        info["stage"] = "review"
                        preview = [{"sql": r["sql"], "columns": r["columns"], "rows": r["rows"][:20],
                                    "total_rows": len(r["rows"])} for r in results]
                        evidence = {"executed_queries": preview, "bindings": bindings, "checks": checks}
                        review = review_plan(self.backend, payload, plan, evidence,
                                             self.deadline, self.config.model.plan_tokens)
                        info["review"] = review.model_dump()
                        if review.verdict != "accept":
                            raise ValueError("Semantic review " + review.verdict + ": " + "; ".join(review.issues))
                info["stage"] = "complete"
                break
            except BackendMemoryError:
                raise
            except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
                record = {"id": question.id, "type": "abstain"}
                detail = str(exc)
                if isinstance(exc, ValidationError):
                    detail = "; ".join(
                        f"{'.'.join(map(str, item['loc'])) or 'plan'}: {item['msg']}"
                        for item in exc.errors(include_input=False, include_url=False)
                    )
                info["error"] = f"{type(exc).__name__}: {detail}"
                prior = {"stage": info["stage"], "error": info["error"], "previous_plan": info.get("plan")}
                if "review" in info:
                    prior["review"] = info["review"]
                logger.warning("%s attempt %d (%s): %s", question.id, attempt + 1, info["stage"], info["error"])
                if isinstance(exc, TimeoutError):
                    break
        log.update({"final_answer": record, "elapsed_seconds": time.monotonic() - started,
                    "model_stats": getattr(self.backend, "last_stats", {})})
        return record, log

    def predict(self, businesses=None, limit=None, *, offset=0):
        if self.backend is None and not self.verified_only:
            raise ValueError("Prediction requires a model backend")
        questions = read_questions(self.config.paths.split, businesses, limit, offset=offset)
        if not questions:
            raise ValueError("No selected questions")
        root = Path(self.config.paths.split)
        data_manifest = {b: manifest(root / "data" / b) for b in sorted({q.business_id for q in questions})}
        fingerprint = digest({"config": self.config.model_dump(), "code": self.code_hash, "model": self.model_hash,
                              "backend": getattr(self.backend, "identity", "verified-only"), "rules": self.rules.hash,
                              "calendar": file_hash(self.calendar_path), "questions": file_hash(root / "questions.jsonl"),
                              "data": data_manifest, "environment": {k: v for k, v in self.environment.items() if k != "free_disk"}})
        if not self.config.runtime.resume:
            fingerprint += "_" + str(time.time_ns())
        journal = Journal(self.config.paths.work, fingerprint, self.config.model_dump())
        write_json(journal.root / "manifest.json", {"fingerprint": fingerprint, "model": self.model_hash, "code": self.code_hash, "data": data_manifest, "environment": self.environment})
        write_json(journal.root / "selection.json", {"offset": offset, "limit": limit,
                                                    "businesses": businesses, "ids": [q.id for q in questions]})
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
                except (TimeoutError, BackendMemoryError):
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
                    except BackendMemoryError as exc:
                        logger.error("GPU inference stopped at %s: %s", q.id, exc)
                        raise
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


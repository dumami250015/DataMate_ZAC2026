import argparse
import logging
from pathlib import Path
from .config import Config
from .evaluation import evaluate, business_folds, read_jsonl
from .output import validate_record
from .pipeline import Agent, read_questions
from .runtime import environment
from .util import dumps, write_json


def main():
    parser = argparse.ArgumentParser(description="DataMate offline agent")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("predict", "prepare", "smoke"):
        p = sub.add_parser(command)
        p.add_argument("--config", required=True)
        p.add_argument("--split"); p.add_argument("--model"); p.add_argument("--work")
        p.add_argument("--business", action="append", dest="businesses")
        p.add_argument("--limit", type=int)
        if command == "prepare":
            p.add_argument("--raw-only", action="store_true")
    p = sub.add_parser("evaluate")
    p.add_argument("--predictions", required=True); p.add_argument("--answers", required=True)
    p.add_argument("--questions"); p.add_argument("--output"); p.add_argument("--selected-only", action="store_true")
    p = sub.add_parser("folds")
    p.add_argument("--questions", required=True); p.add_argument("--output", required=True)
    p.add_argument("--count", type=int, default=5)
    p = sub.add_parser("validate")
    p.add_argument("--submission", required=True); p.add_argument("--questions", required=True)
    p = sub.add_parser("preflight")
    p.add_argument("--work", default=".")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "evaluate":
        print(dumps(evaluate(args.predictions, args.answers, args.questions, args.output, args.selected_only), indent=2)); return
    if args.command == "folds":
        if args.count < 2:
            parser.error("fold count must be >=2")
        write_json(args.output, business_folds(args.questions, args.count)); return
    if args.command == "preflight":
        Path(args.work).mkdir(parents=True, exist_ok=True)
        print(dumps(environment(args.work), indent=2)); return
    if args.command == "validate":
        pred, qs = read_jsonl(args.submission), read_jsonl(args.questions)
        for record in pred.values():
            validate_record(record)
        if set(pred) != set(qs):
            raise ValueError(f"ID mismatch: {len(set(qs)-set(pred))} missing, {len(set(pred)-set(qs))} extra")
        print(f"Validated {len(pred)} records"); return
    config = Config.load(args.config, split=args.split, model=args.model, work=args.work)
    if args.limit is not None and args.limit <= 0:
        parser.error("limit must be positive")
    agent = Agent(config, raw_only=getattr(args, "raw_only", False))
    if args.command == "smoke":
        print("Backend loaded and structured generation passed"); return
    if args.command == "prepare":
        qs = read_questions(config.paths.split, args.businesses, args.limit)
        for business in sorted({q.business_id for q in qs}):
            context = agent.prepare(business)
            print(business, context["database"], len(context["catalog"]))
        return
    print(agent.predict(args.businesses, args.limit))


if __name__ == "__main__":
    main()


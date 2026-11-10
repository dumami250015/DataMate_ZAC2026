from pathlib import Path
import duckdb
import pytest
from datamate.config import Config
from datamate.contracts import TableMapping, Plan, Question, Review
from datamate.storage import build_business
from datamate.rules import Rulebook
from datamate.execution import execute_plan, run_sql
from datamate.output import serialize
from datamate.pipeline import Agent
from datamate.util import write_json, read_json


class MappingBackend:
    identity = "test-mapping"
    last_stats = {}
    def __init__(self, mapping):
        self.mapping = mapping
    def structured(self, system, payload, schema, **kwargs):
        if schema is TableMapping:
            return TableMapping.model_validate(self.mapping)
        if schema is Review:
            return Review(verdict="accept")
        raise AssertionError(schema)
    def smoke_test(self, deadline=None):
        pass


def config(tmp_path):
    return Config(paths={"split": str(tmp_path), "work": str(tmp_path / "work")}, runtime={"sql_timeout_seconds": 30})


def rules(tmp_path):
    p = tmp_path / "rulebook.md"
    p.write_text("# Rules\nRevenue excludes entire returned orders, including later returns.\n", encoding="utf-8")
    return Rulebook(p)


def test_ingestion_fill_duplicates_and_parse_errors(tmp_path):
    root = tmp_path / "business"; root.mkdir()
    (root / "sales.csv").write_text("Order,Name,Amount\nA,One,10\nA,,20\nA,,20\nB,,30\n", encoding="utf-8")
    mapper = MappingBackend({"role": "sales", "grain": "line", "columns": [
        {"source": "Order", "semantic": "order_id"}, {"source": "Name", "semantic": "customer_name"},
        {"source": "Amount", "semantic": "line_amount", "kind": "number"}],
        "group_key": "Order", "fill_columns": ["Name"], "deduplicate_within_order": True})
    ctx = build_business(root, tmp_path / "cache", rules(tmp_path), mapper)
    con = duckdb.connect(ctx["database"], read_only=True)
    assert con.execute("SELECT COUNT(*) FROM t000").fetchone()[0] == 3
    assert con.execute("SELECT customer_name FROM t000 WHERE order_id='B'").fetchone()[0] is None
    assert con.execute("SELECT SUM(line_amount) FROM t000 WHERE order_id='A'").fetchone()[0] == 30
    assert con.execute("SELECT COUNT(*) FROM t000_all").fetchone()[0] == 4
    con.close()


def test_orderless_duplicates_retained(tmp_path):
    root = tmp_path / "business"; root.mkdir()
    (root / "ledger.csv").write_text("Item,Amount\nA,10\nA,10\n")
    backend = MappingBackend({"columns": [{"source": "Item", "semantic": "product_name"}, {"source": "Amount", "semantic": "amount", "kind": "number"}]})
    ctx = build_business(root, tmp_path / "cache", rules(tmp_path), backend)
    con = duckdb.connect(ctx["database"], read_only=True)
    assert con.execute("SELECT SUM(amount) FROM t000").fetchone()[0] == 20
    con.close()


def test_worker_revenue_late_returns_and_debt_no_join_multiplication(tmp_path):
    db = tmp_path / "test.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE orders(id VARCHAR, d DATE, payable INTEGER, status VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('A','2026-01-01',100,'returned'), ('B','2026-01-02',200,'done')")
    con.execute("CREATE TABLE lines(id VARCHAR, amount INTEGER)")
    con.execute("INSERT INTO lines VALUES ('A',40),('A',60),('B',80),('B',120)")
    con.execute("CREATE TABLE payments(id VARCHAR, d DATE, amount INTEGER)")
    con.execute("INSERT INTO payments VALUES ('A','2026-01-01',10),('A','2026-01-05',20),('A','2026-03-01',30)")
    con.close()
    cfg = config(tmp_path)
    allowed = {"orders", "lines", "payments"}
    revenue = run_sql(db, "SELECT SUM(l.amount) FROM lines l JOIN orders o ON l.id=o.id WHERE o.status <> 'returned'", {}, allowed, cfg, tmp_path / "tmp")
    assert revenue["rows"] == [[200]]
    debt = run_sql(db, "WITH p AS (SELECT id,SUM(amount) paid FROM payments WHERE d <= DATE '2026-01-31' GROUP BY id) SELECT SUM(GREATEST(o.payable-COALESCE(p.paid,0),0)) FROM orders o LEFT JOIN p ON o.id=p.id", {}, allowed, cfg, tmp_path / "tmp")
    assert debt["rows"] == [[270]]


def test_metric_compile_execution(tmp_path):
    db = tmp_path / "db.duckdb"
    con = duckdb.connect(str(db)); con.execute("CREATE TABLE t000(k VARCHAR,n VARCHAR,q INTEGER)")
    con.execute("INSERT INTO t000 VALUES ('B','Second',3),('A','First',3)"); con.close()
    plan = Plan(route="metric", metric="rank", tables=["t000"], queries=[{"sql": "SELECT k entity_key,n entity_name,q value FROM t000", "purpose": "rank"}], output={"type": "string"})
    context = {"database": str(db), "catalog": [{"table": "t000", "parse_errors": {}}]}
    q = Question(id="q", business_id="b", question="top", as_of_date="2026-03-04")
    results, _, _ = execute_plan(plan, q, context, config(tmp_path), None)
    assert serialize(q.id, plan, results)["answer"] == "First"


def test_full_pipeline_and_resume(tmp_path):
    split = tmp_path / "split"; business = split / "data" / "b1"; business.mkdir(parents=True)
    (business / "data.csv").write_text("Amount\n10\n20\n")
    (split / "questions.jsonl").write_text('{"id":"q1","business_id":"b1","question":"total","as_of_date":"2026-01-01"}\n')
    (split / "rulebook.md").write_text("Revenue is sum of amounts.\n")
    (split / "calendar.csv").write_text("date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n2026-01-01,4,13,11,2025,false,\n")
    class Fake(MappingBackend):
        calls = 0
        def structured(self, system, payload, schema, **kwargs):
            if schema is Plan:
                self.calls += 1
                return Plan(route="metric", metric="sum", tables=["t000"], rule_refs=["R001"], required_columns={"t000": ["amount"]},
                            queries=[{"sql": "SELECT amount value FROM t000", "purpose": "sum"}], output={"type": "number"})
            return super().structured(system, payload, schema, **kwargs)
    backend = Fake({"columns": [{"source": "Amount", "semantic": "amount", "kind": "number"}]})
    cfg = Config(paths={"split": str(split), "work": str(tmp_path / "work")})
    agent = Agent(cfg, backend=backend)
    run = agent.predict()
    assert read_json(run / "results" / "q1.json")["answer"] == 30
    agent.predict()
    assert backend.calls == 1


from pathlib import Path
import duckdb
import pytest
from datamate.config import Config
from datamate.contracts import TableMapping, Plan, Question, Review
from datamate.storage import build_business, bulk_insert
from datamate.rules import Rulebook
from datamate.execution import execute_plan, run_sql
from datamate.llm import BackendMemoryError
from datamate.output import serialize
from datamate.pipeline import Agent, read_questions
from datamate.util import dumps, write_json, read_json


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


def test_stable_line_identity_removes_only_exact_export_duplicates(tmp_path):
    root = tmp_path / "business"
    root.mkdir()
    (root / "lines.csv").write_text("Order,Line,Amount\nA,1,10\nA,1,10\nA,2,10\nA,,10\nA,,10\n", encoding="utf-8")
    backend = MappingBackend({"columns": [
        {"source": "Order", "semantic": "order_id"},
        {"source": "Line", "semantic": "line_number"},
        {"source": "Amount", "semantic": "line_amount", "kind": "number"},
    ]})
    ctx = build_business(root, tmp_path / "cache", rules(tmp_path), backend)
    assert ctx["catalog"][0]["duplicate_rows"] == 1
    with duckdb.connect(ctx["database"], read_only=True) as con:
        assert con.execute("SELECT COUNT(*), SUM(line_amount) FROM t000").fetchone() == (4, 40)
        assert con.execute("SELECT COUNT(*) FROM t000_all").fetchone()[0] == 5


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


def test_named_date_parameters(tmp_path):
    db = tmp_path / "dates.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE t000(d DATE, amount INTEGER)")
    con.execute("INSERT INTO t000 VALUES ('2026-03-01',10),('2026-03-04',20),('2026-03-05',30)")
    con.close()
    plan = Plan(route="metric", metric="sum", tables=["t000"], time={"kind": "this_month"}, queries=[{"sql": "SELECT amount value FROM t000 WHERE d >= $start AND d < $end", "purpose": "month to date"}])
    context = {"database": str(db), "catalog": [{
        "table": "t000", "parse_errors": {}, "mapping": {"coverage_end": "2026-02-28"},
        "observed_dates": {"d": ["2026-03-01", "2026-03-05"]},
    }]}
    q = Question(id="q", business_id="b", question="total", as_of_date="2026-03-04")
    results, _, _ = execute_plan(plan, q, context, config(tmp_path), None)
    assert serialize(q.id, plan, results)["answer"] == 30


def test_worker_binds_only_parameters_recognized_by_duckdb(tmp_path):
    db = tmp_path / "params.duckdb"
    duckdb.connect(str(db)).close()
    result = run_sql(db, "SELECT $END AS actual, '$start' AS label -- $start\n",
                     {"start": "ignored", "end": "2026-06-01"}, set(), config(tmp_path), tmp_path / "tmp")
    assert result["rows"] == [["2026-06-01", "$start"]]


def test_numeric_verification_check_rejects_status_text(tmp_path):
    db = tmp_path / "verification.duckdb"
    with duckdb.connect(str(db)) as connection:
        connection.execute("CREATE TABLE t000 AS SELECT 'cancelled' AS status")
    plan = Plan(
        route="sql",
        tables=["t000"],
        queries=[{"sql": "SELECT COUNT(*) FROM t000", "purpose": "cancelled count"}],
        checks=[{"sql": "SELECT status FROM t000", "description": "check count", "expectation": "positive"}],
        output={"type": "number"},
    )
    question = Question(id="q", business_id="b", question="cancelled count", as_of_date="2026-03-04")
    context = {"database": str(db), "catalog": [{"table": "t000", "parse_errors": {}}]}
    with pytest.raises(ValueError, match="Verification check must return a number"):
        execute_plan(plan, question, context, config(tmp_path), None)


@pytest.fixture
def mini_split(tmp_path):
    split = tmp_path / "split"; business = split / "data" / "b1"; business.mkdir(parents=True)
    (business / "data.csv").write_text("Amount\n10\n20\n")
    (split / "questions.jsonl").write_text('{"id":"q1","business_id":"b1","question":"total","as_of_date":"2026-01-01"}\n')
    (split / "rulebook.md").write_text("Revenue is sum of amounts.\n")
    (split / "calendar.csv").write_text("date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n2026-01-01,4,13,11,2025,false,\n")
    return split


def test_full_pipeline_and_resume(tmp_path, mini_split):
    class Fake(MappingBackend):
        calls = 0
        def structured(self, system, payload, schema, **kwargs):
            if schema is Plan:
                self.calls += 1
                return Plan(route="metric", metric="sum", tables=["t000"], rule_refs=["R001"], required_columns={"t000": ["amount"]},
                            queries=[{"sql": "SELECT amount value FROM t000", "purpose": "sum"}], output={"type": "number"})
            return super().structured(system, payload, schema, **kwargs)
    backend = Fake({"columns": [{"source": "Amount", "semantic": "amount", "kind": "number"}]})
    cfg = Config(paths={"split": str(mini_split), "work": str(tmp_path / "work")})
    agent = Agent(cfg, backend=backend)
    run = agent.predict()
    assert read_json(run / "results" / "q1.json")["answer"] == 30
    agent.predict()
    assert backend.calls == 1


def test_offset_is_applied_after_filtering_and_preserves_order(mini_split):
    with (mini_split / "questions.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"id":"q2","business_id":"b2","question":"two","as_of_date":"2026-01-01"}\n')
        f.write('{"id":"q3","business_id":"b1","question":"three","as_of_date":"2026-01-01"}\n')
    assert [q.id for q in read_questions(mini_split, ["b1"], 1, offset=1)] == ["q3"]


def test_verified_only_flat_export_with_subtotal_and_money_shorthand(tmp_path, mini_split):
    business = mini_split / "data" / "b1"
    (business / "sales.csv").write_text(
        "Bill,Date,State,Net,Product,Buyer\n"
        "A,01/01/2026,xong,5tr520,One,Customer\n"
        "A,01/01/2026,xong,5tr520,One,Customer\n"
        ",,,8902k,,TỔNG NGÀY 01/01\n", encoding="utf-8",
    )
    (business / "README.md").write_text(
        "## Cột trong `sales.csv`\n"
        "- **Bill**: mã đơn\n- **Date**: ngày bán\n- **State**: trạng thái đơn\n"
        "- **Net**: thành tiền sau giảm\n- **Product**: tên hàng\n- **Buyer**: tên khách\n"
        "## Quy ước\nDữ liệu có đến hết 01/01/2026.\n", encoding="utf-8",
    )
    (mini_split / "rulebook.md").write_text(
        "Đơn thuộc kỳ nào theo ngày đặt.\n"
        "Đơn tính vào doanh thu không huỷ và không trả hàng, đang giao vẫn tính.\n"
        "Doanh thu một đơn là thành tiền các dòng hàng đã trừ giảm giá trên từng dòng.\n"
        "Hai dòng của cùng một đơn giống nhau ở mọi ô thì tính một lần.\n", encoding="utf-8",
    )
    (mini_split / "questions.jsonl").write_text(dumps({
        "id": "q1", "business_id": "b1", "question": "Tổng doanh thu hôm nay là bao nhiêu?", "as_of_date": "2026-01-01",
    }) + "\n", encoding="utf-8")
    cfg = Config(paths={"split": str(mini_split), "work": str(tmp_path / "work")},
                 runtime={"sql_timeout_seconds": 90})
    agent = Agent(cfg, verified_only=True)
    run = agent.predict()
    assert read_json(run / "results" / "q1.json")["answer"] == 5_520_000
    table, = agent.prepare("b1")["catalog"]
    assert (table["row_count"], table["duplicate_rows"], table["subtotal_rows"]) == (1, 1, 1)


def test_pipeline_repairs_abstentions_and_invalid_sql(tmp_path, mini_split):
    class Fake(MappingBackend):
        plans = 0
        reviews = 0

        def structured(self, system, payload, schema, **kwargs):
            if schema is Plan:
                self.plans += 1
                if self.plans == 1:
                    return Plan(route="abstain", reason="Unclear amount column")
                expected = {2: "planning", 3: "validation", 4: "review"}
                assert payload["repair"]["stage"] == expected[self.plans]
                sql = "SELECT SUM( FROM t000" if self.plans == 2 else "SELECT SUM(amount) FROM t000"
                return Plan(tables=["t000"], rule_refs=["R001"], queries=[{"sql": sql, "purpose": "sum"}])
            if schema is Review:
                self.reviews += 1
                if self.reviews == 1:
                    return Review(verdict="abstain", issues=["Recheck the answer against the amount column"])
            return super().structured(system, payload, schema, **kwargs)

    backend = Fake({"columns": [{"source": "Amount", "semantic": "amount", "kind": "number"}]})
    cfg = Config(paths={"split": str(mini_split), "work": str(tmp_path / "work")},
                 pipeline={"semantic_repairs": 3}, runtime={"fail_fast": True})
    run = Agent(cfg, backend=backend).predict()
    assert read_json(run / "results" / "q1.json") == {"id": "q1", "type": "number", "answer": 30}
    attempts = read_json(run / "logs" / "q1.json")["attempts"]
    assert [attempt["stage"] for attempt in attempts] == ["planning", "validation", "review", "complete"]


def test_gpu_oom_stops_the_run_without_committing_abstentions(tmp_path, mini_split):
    class Fake(MappingBackend):
        def structured(self, system, payload, schema, **kwargs):
            if schema is Plan:
                raise BackendMemoryError("CUDA allocation failed")
            return super().structured(system, payload, schema, **kwargs)

    work = tmp_path / "work"
    cfg = Config(paths={"split": str(mini_split), "work": str(work)})
    with pytest.raises(BackendMemoryError, match="CUDA allocation failed"):
        Agent(cfg, backend=Fake({})).predict()
    run = next((work / "runs").iterdir())
    assert not (run / "results" / "q1.json").exists()
    assert read_json(run / "progress.json") == {"total": 1, "completed": 0}


def test_bulk_insert_does_not_narrow_decimal_width(tmp_path):
    from decimal import Decimal

    db = tmp_path / "decimal.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE amounts(value DECIMAL(38,8))")
    bulk_insert(
        con,
        "amounts",
        [[Decimal("99999")], [Decimal("100000")], [Decimal("123456789012345.125")]],
    )
    values = [row[0] for row in con.execute("SELECT value FROM amounts ORDER BY value").fetchall()]
    con.close()
    assert values == [
        Decimal("99999.00000000"),
        Decimal("100000.00000000"),
        Decimal("123456789012345.12500000"),
    ]


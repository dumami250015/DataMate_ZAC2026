from datetime import date
import pytest
from datamate.contracts import Plan, Question
from datamate.config import Config
from datamate.execution import execute_plan, validate_sql
from datamate.planning import ground_plan, review_plan
from datamate.calendar import resolve
from datamate.contracts import Review, TimeSpec
from datamate.llm import GenerationError
from datamate.output import serialize
from datamate.verified_intent import parse_intent


def test_date_functions_are_safe_normalized_ast_names():
    sql = "SELECT date_diff('day', a, b), strftime(a,'%Y-%m'), regexp_matches(c,'x'), string_agg(c,',') FROM t000"
    assert validate_sql(sql, {"t000"}) == {"t000"}


def test_generated_cte_metadata_is_replaced_by_physical_tables():
    plan = Plan(tables=["order_revenue"], required_columns={"order_revenue": ["amount"]},
                queries=[{"sql": "WITH order_revenue AS (SELECT amount FROM t000) SELECT SUM(amount) FROM order_revenue"}])
    context = {"catalog": [{"table": "t000"}]}
    grounded = ground_plan(plan, context)
    assert grounded.tables == ["t000"]
    assert grounded.required_columns == {}
    with pytest.raises(ValueError, match="order_revenue"):
        ground_plan(Plan(queries=[{"sql": "SELECT SUM(amount) FROM order_revenue"}]), context)
    with pytest.raises(ValueError, match="hidden"):
        validate_sql("SELECT * FROM hidden WHERE EXISTS (WITH hidden AS (SELECT * FROM t000) SELECT * FROM hidden)", {"t000"})


def test_quoted_temporal_parameters_bind_the_explicit_question_day():
    context = {"catalog": [{"table": "t000", "columns": [
        {"name": "order_date", "kind": "date"}, {"name": "label", "kind": "text"},
    ]}]}
    plan = Plan(time={"kind": "range", "start": "2026-12-26", "end": "2026-12-26"},
                queries=[{"sql": "SELECT COUNT(*) FROM t000 o WHERE o.order_date >= '$start' "
                                 "AND o.order_date < CAST('$end' AS DATE) AND o.label <> '$start'"}])
    q = Question(id="q", business_id="b", question="Hôm 26/12/2025 giá bao nhiêu?", as_of_date="2026-09-07")
    grounded = ground_plan(plan, context, q)
    assert grounded.time.start == grounded.time.end == "2025-12-26"
    sql = grounded.queries[0].sql
    assert ">= $start" in sql and "CAST($end AS DATE)" in sql
    assert "<> '$start'" in sql


def test_explicit_quarter_and_cancellation_wording():
    revenue = parse_intent("Quý 1 năm 2026 cửa hàng bán đc tổng cộng bao nhiêu tiền?")
    assert (revenue.kind, revenue.time.kind, revenue.time.quarter) == ("sales", "quarter", 1)
    cancelled = parse_intent("Tháng 4/2025 bị huỷ mấy đơn?")
    assert (cancelled.kind, cancelled.time.year, cancelled.time.month) == ("cancelled", 2025, 4)


def test_metric_scalar_pair_uses_sql_route():
    plan = Plan(route="metric", postprocess="ratio_percent",
                queries=[{"sql": "SELECT SUM(amount) FROM t000"}, {"sql": "SELECT SUM(amount) FROM t001"}])
    assert plan.route == "sql"


def test_verified_totals_require_unqualified_question():
    sales = parse_intent("Cho hỏi hôm qua riêng ca sáng bán được bao nhiêu tiền?")
    assert (sales.kind, sales.time.kind, sales.shift) == ("sales", "yesterday", "sang")
    purchase = parse_intent("Tháng trước tổng tiền phiếu nhập hàng là bao nhiêu?")
    assert (purchase.kind, purchase.time.kind, purchase.shift) == ("purchase", "previous_month", None)
    assert parse_intent("Hôm qua tổng doanh thu khách lẻ là bao nhiêu?") is None
    assert parse_intent("Hôm qua và hôm nay tổng doanh thu là bao nhiêu?") is None


def test_verified_intent_preserves_month_shift_and_rejects_other_filters():
    intent = parse_intent("Đếm số đơn bán trong ca sáng tháng 8/2025")
    assert (intent.kind, intent.shift, intent.time.month, intent.time.year) == ("orders", "sang", 8, 2025)
    assert parse_intent("Đếm số đơn bán của khách thân thiết trong ca sáng tháng 8/2025") is None


def test_generated_dual_constant_cte_and_explicit_list_projection():
    plan = Plan(output={"type": "list", "answer_column": "name"}, queries=[{
        "sql": "WITH cutoff AS (SELECT $start AS cutoff_date FROM dual) SELECT name, n FROM t000 CROSS JOIN cutoff",
    }])
    grounded = ground_plan(plan, {"catalog": [{"table": "t000"}]})
    assert grounded.tables == ["t000"]
    assert "dual" not in grounded.queries[0].sql.lower()
    record = serialize("q", grounded, [{"columns": ["name", "n"], "rows": [["One", 9], ["Two", 3]]}])
    assert record["answer"] == ["One", "Two"]


def test_vietnamese_date_range_normalizes_to_exclusive_iso_end():
    spec = TimeSpec(kind="range", start="01/06/2026", end="30/06/2026")
    assert resolve(spec, date(2026, 9, 7)) == ("2026-06-01", "2026-07-01")


def test_invalid_review_json_retries_with_the_same_executed_evidence():
    evidence = {"executed_queries": [{"sql": "SELECT SUM(amount) FROM t000"}],
                "bindings": {"start": "2026-06-01", "end": "2026-07-01"}}

    class Reviewer:
        def structured(self, system, payload, schema, **kwargs):
            assert payload["result"] == evidence
            if "format_retry" not in payload:
                raise GenerationError("Malformed JSON")
            return Review(verdict="accept")

    plan = Plan(queries=[{"sql": "SELECT SUM(amount) FROM t000"}])
    assert review_plan(Reviewer(), {}, plan, evidence).verdict == "accept"


def test_final_snapshot_cannot_answer_historical_balance(tmp_path):
    plan = Plan(tables=["t000"], queries=[{"sql": "SELECT SUM(snapshot_quantity) FROM t000", "purpose": "stock"}], time={"kind": "as_of"})
    q = Question(id="q", business_id="b", question="stock", as_of_date="2026-01-01")
    context = {"database": "never-opened", "catalog": [{"table": "t000", "columns": [{"name": "snapshot_quantity"}], "parse_errors": {}, "mapping": {"coverage_end": "2026-02-01"}}]}
    cfg = Config(paths={"split": str(tmp_path), "work": str(tmp_path / "work")})
    with pytest.raises(ValueError, match="snapshot"):
        execute_plan(plan, q, context, cfg, None)


def test_period_outside_coverage_cannot_return_zero(tmp_path):
    plan = Plan(tables=["t000"], queries=[{"sql": "SELECT 0 FROM t000", "purpose": "revenue"}], time={"kind": "month", "year": 2027, "month": 1})
    q = Question(id="q", business_id="b", question="revenue", as_of_date="2027-01-31")
    context = {"database": "never-opened", "catalog": [{"table": "t000", "parse_errors": {}, "mapping": {"coverage_end": "2026-02-01"}}]}
    cfg = Config(paths={"split": str(tmp_path), "work": str(tmp_path / "work")})
    with pytest.raises(ValueError, match="coverage"):
        execute_plan(plan, q, context, cfg, None)


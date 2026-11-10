from datetime import date
import pytest
from datamate.contracts import Plan, Question
from datamate.config import Config
from datamate.execution import execute_plan, validate_sql


def test_date_functions_are_safe_normalized_ast_names():
    sql = "SELECT date_diff('day', a, b), strftime(a,'%Y-%m'), regexp_matches(c,'x'), string_agg(c,',') FROM t000"
    assert validate_sql(sql, {"t000"}) == {"t000"}


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


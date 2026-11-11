from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock
import pytest
from datamate.calendar import resolve
from datamate.contracts import TimeSpec, Plan, Question
from datamate.normalize import infer_decimal_separator, number, parse_date
from datamate.output import serialize, validate_record, Journal
from datamate.evaluation import score_one, table_equal
from datamate.execution import validate_sql
from datamate.config import ModelConfig
from datamate.llm import Backend, BackendMemoryError, TransformersBackend, complete_json_prefix, extract_json


def test_numbers_are_not_silently_reinterpreted():
    assert number("25.000đ", ",") == 25000
    assert number("1,500.0000", ".") == 1500
    assert number("1 kg") == 1
    assert number("(12,50)", ",") == Decimal("-12.50")
    with pytest.raises(ValueError):
        number("189,000")
    with pytest.raises(ValueError):
        number("NaN")


def test_thousands_format_can_be_learned_from_unambiguous_cells():
    separator = infer_decimal_separator(["2,880,000", "878,400"], ",")
    assert separator == "."
    assert number("878,400", separator) == 878400
    assert infer_decimal_separator(["1,234"], "auto") == "auto"
    assert infer_decimal_separator(["2,880,000", "12,50"], ".") == "auto"
    with pytest.raises(ValueError):
        number("12,50", separator)


def test_dates_and_boundaries():
    d = date(2026, 3, 4)
    assert resolve(TimeSpec(kind="previous_week"), d) == ("2026-02-23", "2026-03-02")
    assert resolve(TimeSpec(kind="last_n_days", n=7), d) == ("2026-02-26", "2026-03-05")
    assert resolve(TimeSpec(kind="previous_month_to_date"), date(2026, 3, 31)) == ("2026-02-01", "2026-03-01")
    assert resolve(TimeSpec(kind="month", month=12), d) == ("2025-12-01", "2026-01-01")
    assert parse_date("1 thg 6 2025") == "2025-06-01"
    assert parse_date("31/12", year=2025) == "2025-12-31"
    with pytest.raises(ValueError):
        parse_date("31/12")


def test_tet_uses_supplied_calendar(tmp_path):
    p = tmp_path / "calendar.csv"
    p.write_text("date,lunar_day,lunar_month,lunar_year,lunar_leap\n2026-02-10,23,12,2025,false\n2026-02-26,10,1,2026,false\n")
    assert resolve(TimeSpec(kind="tet", year=2026), date(2026, 3, 1), p) == ("2026-02-10", "2026-02-27")


def plan(**kwargs):
    return Plan(queries=[{"sql": "SELECT 1", "purpose": "test"}], **kwargs)


def test_exact_rounding_and_ratio():
    p = Plan(queries=[{"sql": "SELECT 1", "purpose": "a"}, {"sql": "SELECT 1", "purpose": "b"}], postprocess="growth_percent", output={"type": "percent"})
    rows = [{"rows": [[100]]}, {"rows": [[120]]}]
    assert serialize("q", p, rows)["answer"] == Decimal("-16.67")
    p = plan(output={"type": "number", "decimals": 0})
    assert serialize("q", p, [{"rows": [[Decimal("2.5")]]}])["answer"] == 3
    with pytest.raises(ValueError):
        serialize("q", p, [{"rows": [[None]]}])


def test_strict_output_and_scorer():
    with pytest.raises(ValueError):
        validate_record({"id": "q", "type": "number", "answer": True})
    ref = {"id": "q", "type": "string", "answer": "Cà phê sữa", "alias": ["SP001"]}
    assert score_one({"id": "q", "type": "string", "answer": "sp001"}, ref) == 1
    assert score_one({"id": "q", "type": "string", "answer": "Ca phe sua"}, ref) == -1
    assert score_one(None, ref) == 0
    assert score_one({"id": "q", "type": "number", "answer": 1000000001}, {"id": "q", "type": "number", "answer": 1000000000}) == -1
    assert table_equal({"columns": ["amount", "name"], "rows": [[2, "B"], [1, "A"]]}, {"columns": ["name", "amount"], "rows": [["A", 1], ["B", 2]]})
    assert score_one({"id": "q", "type": "list", "answer": ["SP1", "SP2"]}, {"id": "q", "type": "list", "answer": ["Cà phê", "Trà"], "alias": [["SP1"], ["SP2"]]}) == 1
    assert table_equal({"columns": ["name", "money"], "rows": [["KH1", 10]]}, {"columns": ["name", "money"], "rows": [["Lan", 10]]}, [["KH1"]])


@pytest.mark.parametrize("sql", ["DELETE FROM t000", "SELECT * FROM read_csv('/etc/passwd')", "SELECT * FROM '/tmp/a.csv'", "SELECT getenv('HOME')", "SELECT 1; SELECT 2", "ATTACH '/tmp/x' AS x", "SELECT * FROM other"])
def test_sql_rejects_unsafe_or_unknown_access(sql):
    with pytest.raises(ValueError):
        validate_sql(sql, {"t000"})


def test_sql_cte_and_parameters():
    assert validate_sql("WITH x AS (SELECT * FROM t000 WHERE d >= $start) SELECT SUM(v) FROM x", {"t000"}) == {"t000"}


def test_json_rejects_truncated_reasoning():
    assert extract_json('<think>draft</think>{"a":1}') == {"a": 1}
    with pytest.raises(RuntimeError):
        extract_json('<think>{"a":1}')
    with pytest.raises(RuntimeError):
        extract_json('{"a":1} trailing')


def test_structured_generation_reads_complete_object_with_wrappers():
    class Fake(Backend):
        def generate(self, messages, **kwargs):
            assert kwargs["structured_output"]
            return 'Here is the result:\n```json\n{"output":{"type":"unanswerable"},"evidence":["No payment dates"]}\n```'

    result = Fake().structured("Plan the answer", {}, Plan)
    assert result.route == "unanswerable"
    assert result.evidence == ["No payment dates"]
    assert complete_json_prefix('{"answer":\n{"a": 1}') is None
    assert complete_json_prefix('{"a":1}', thinking=True) is None
    assert complete_json_prefix('analysis</think>\n{"a":1}', thinking=True) == '{"a":1}'


@pytest.mark.parametrize("length,oom", [(512, False), (513, False), (513, True)])
def test_transformers_generation_uses_chunked_prefill_for_long_prompts(length, oom, monkeypatch):
    class CudaOutOfMemory(RuntimeError):
        pass

    backend = TransformersBackend.__new__(TransformersBackend)
    backend.config = ModelConfig(prefill_chunk_size=512)
    backend.torch = SimpleNamespace(inference_mode=nullcontext, cuda=SimpleNamespace(
        max_memory_allocated=lambda: 0, OutOfMemoryError=CudaOutOfMemory,
    ))
    tensor = Mock(shape=(1, length))
    backend.tokenizer = Mock(return_value={"input_ids": tensor}, pad_token_id=1, eos_token_id=2,
                             pad_token="<pad>", eos_token="<eos>")
    backend.tokenizer.decode.return_value = "{}<eos>"
    backend.model = Mock()
    output = MagicMock()
    output.__getitem__.return_value = [2]
    backend.model.generate.return_value = output
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(StoppingCriteriaList=list))
    if oom:
        backend.model.generate.side_effect = CudaOutOfMemory("test allocation failed")
        with pytest.raises(BackendMemoryError, match="513 prompt tokens"):
            backend.generate([], structured_output=True)
    else:
        assert backend.generate([], structured_output=True) == "{}"
    options = backend.model.generate.call_args.kwargs
    assert options["input_ids"] is tensor.to.return_value
    assert options.get("prefill_chunk_size") == (512 if length > 512 else None)
    assert not options["stopping_criteria"][0](Mock(shape=(1, length + 7)), None)
    ids = MagicMock(shape=(1, length + 8))
    assert options["stopping_criteria"][0](ids, None)


def test_atomic_journal_ignores_partial_result(tmp_path):
    q = Question(id="q1", business_id="b1", question="x", as_of_date="2026-01-01")
    journal = Journal(tmp_path, "hash", {})
    journal.commit(q.id, {"id": q.id, "type": "number", "answer": 1}, {})
    (journal.root / "results" / "q2.json.tmp").write_text('{')
    assert journal.has("q1") and not journal.has("q2")
    journal.export([q])
    assert (journal.root / "submission.jsonl").read_text().count("\n") == 1


def test_mixed_table_rounding():
    p = plan(output={"type": "table", "columns": ["name", "money", "percent"], "column_decimals": [None, 0, 2]})
    result = {"columns": ["name", "money", "percent"], "rows": [["A", Decimal("12.5"), Decimal("3.125")]]}
    assert serialize("q", p, [result])["answer"]["rows"] == [["A", 13, Decimal("3.13")]]


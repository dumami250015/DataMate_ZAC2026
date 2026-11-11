import pytest

from datamate.storage import map_source
from datamate.contracts import TableMapping
from datamate.readers import Source


def test_conflicting_semantics_trigger_mapping_repair(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("Paid,Payable\n10,20\n", encoding="utf-8")
    class Fake:
        calls = 0
        def structured(self, system, payload, schema, **kwargs):
            self.calls += 1
            if self.calls == 2:
                assert "repair" in payload
            return TableMapping(columns=[
                {"source": "Paid", "semantic": "amount" if self.calls == 1 else "paid_amount"},
                {"source": "Payable", "semantic": "amount" if self.calls == 1 else "total_payable"},
            ])
    backend = Fake()
    mapping = map_source(backend, {"profile": {"headers": ["Paid", "Payable"]}}, Source(path, "", "csv"))
    assert backend.calls == 2
    assert [c.semantic for c in mapping.columns] == ["paid_amount", "total_payable"]


def test_identical_repeated_mapping_can_be_deduplicated(tmp_path):
    class Fake:
        def structured(self, *args, **kwargs):
            return TableMapping(columns=[{"source": "A", "semantic": "amount"}] * 2)
    result = map_source(Fake(), {"profile": {"headers": ["A"]}}, Source(tmp_path / "unused.csv", "", "csv"))
    assert len(result.columns) == 1


@pytest.mark.parametrize("options", [
    {"deduplicate_within_order": True},
    {"fill_columns": ["B"]},
    {"columns": [{"source": "A", "semantic": "amount"}, {"source": "B", "semantic": "amount"}]},
])
def test_failed_mapping_repair_preserves_usable_columns(tmp_path, options):
    class Fake:
        calls = 0

        def structured(self, system, payload, schema, **kwargs):
            self.calls += 1
            proposal = {"columns": [{"source": "A", "semantic": "amount"}], **options}
            return TableMapping(**proposal)

    backend = Fake()
    mapping = map_source(backend, {"profile": {"headers": ["A", "B"]}}, Source(tmp_path / "unused.csv", "", "csv"))
    assert backend.calls == 2
    assert [c.source for c in mapping.columns] == ([] if "columns" in options else ["A"])
    assert not mapping.fill_columns and not mapping.deduplicate_within_order
    assert mapping.notes

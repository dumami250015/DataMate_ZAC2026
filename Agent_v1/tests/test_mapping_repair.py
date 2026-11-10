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

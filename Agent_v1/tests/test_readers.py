import sqlite3
from pathlib import Path
import openpyxl
from datamate.readers import discover, profile_rows, preview
from datamate.capabilities import catalog_capabilities


def test_all_workbook_sheets_and_readonly_sqlite(tmp_path):
    workbook = openpyxl.Workbook()
    workbook.active.title = "2025"
    workbook.active.append(["Ngày", "Amount"])
    workbook.active.append(["31/12", 10])
    sheet = workbook.create_sheet("2026")
    sheet.append(["Ngày", "Amount"]); sheet.append(["01/01", 20])
    workbook.save(tmp_path / "book.xlsx")
    con = sqlite3.connect(tmp_path / "data.sqlite")
    con.execute('CREATE TABLE "odd table" (id TEXT, amount INTEGER)')
    con.execute('INSERT INTO "odd table" VALUES (?,?)', ("001", 7))
    con.commit(); con.close()
    sources = discover(tmp_path)
    assert len(sources) == 3
    assert {s.sheet for s in sources} == {"2025", "2026", "odd table"}
    assert all(profile_rows(s)["row_count"] == 1 for s in sources)
    assert preview(next(s for s in sources if s.kind == "sqlite"))[1]["values"][0] == "001"


def test_capability_does_not_treat_empty_column_as_available():
    c = [{"table": "t000", "columns": [{"name": "line_amount"}], "row_count": 10,
          "null_counts": {"line_amount": 10}, "parse_errors": {}}]
    assert catalog_capabilities(c)["line_revenue"]["state"] == "unknown"
    c[0]["null_counts"] = {}
    assert catalog_capabilities(c)["line_revenue"]["state"] == "supported"


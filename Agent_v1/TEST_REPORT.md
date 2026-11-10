# Validation report

Recorded: 11 November 2026 (Asia/Bangkok).

## Completed checks

| Check | Result |
|---|---|
| CPU unit/integration suite | **30 passed**, latest run 18.50 seconds |
| Python source compilation | `datamate/` and `scripts/` compile successfully |
| Kaggle notebook | 11 code cells generated; every code cell parses as Python |
| Scorer reference self-check | All 3,000 released training references score correctly against themselves |
| Real CSV ingestion | `biz_101`: 56,501 data rows, 1 schema-family table |
| Real Excel ingestion | `biz_102`: 86,106 data rows across 19 sheets, 2 schema-family tables |
| Real SQLite ingestion | `biz_123`: 268,911 rows across 17 database tables |

The real-data runs used `prepare --raw-only`: they verify readers, profiling, source preservation, bulk insertion, and cache materialization. They do **not** establish semantic schema mapping accuracy or question-answering accuracy. The reference self-check validates the scorer's handling of released formats; it is not a model score.

## Tests exercised

- Conflicting schema column names trigger a bounded model repair; identical repeated mappings are safely deduplicated.

- Decimal parsing, ambiguous separators, unit suffixes, and nonfinite rejection.
- Vietnamese dates, missing-year dates, previous-week and partial-month boundaries.
- Tết lookup from a supplied calendar.
- Exact rounding and signed percentage changes.
- Mixed money/percentage precision in one table.
- String, list, and table aliases; table column/row ordering; accent preservation.
- SQL mutation, external-read, unknown-relation, and function rejection.
- SQL CTEs, logical predicates, date functions, and named time parameters.
- Rejection of truncated JSON/reasoning output.
- Atomic result commits and interrupted temporary-file handling.
- Group-scoped fill and within-order duplicate handling.
- Preservation of identical orderless ledger rows.
- Revenue exclusion of returned orders and payment preaggregation for debt.
- Ranking tie order.
- Workbook sheet enumeration and read-only SQLite inspection.
- Unknown capabilities for empty required columns.
- Rejection of historical stock answers from a final snapshot at a different date.
- Rejection of zero answers for periods wholly outside documented coverage.
- Complete controller execution and resume with a fake model.
- Decimal bulk transport across inferred precision boundaries (`99999`, `100000`, and a 15-digit fractional value).

## Local test environment

Windows, Python 3.14.0. Local CPU dependencies were installed into `.deps/`, which is excluded from the upload archive.

| Package | Version |
|---|---|
| duckdb | 2.0.0 |
| sqlglot | 30.30.0 |
| pydantic | 2.14.3 |
| PyYAML | 6.0.3 |
| openpyxl | 3.1.5 |
| simplejson | 4.2.0 |
| pandas | 3.0.3 |
| pytest | 9.2.0 |

These versions document the tested CPU environment. They are not a validated Linux/CUDA lockfile for Kaggle.

## Not yet verified

- Actual Qwen3.5-9B load/generation on a Kaggle GPU.
- NF4 and hybrid-attention kernel compatibility for the selected accelerator.
- The optional vLLM backend on target hardware.
- Model-driven schema mapping, Vietnamese planning accuracy, or held-out PA.
- Full-split inference time and peak GPU memory.
- Repeatability between Kaggle NF4 and the organizer's GPU/precision/backend.
- Organizer-approved Docker build and six-hour cold-run limit.

No local model checkpoint or accessible CUDA inference environment was available for those checks. Run the notebook smoke test first, freeze the working environment, then evaluate a held-out training-business subset before a full test submission.

## Reproducing the CPU checks

In a normal prepared Python environment, from `Agent_v1`:

```bash
python -m pytest -q
python -m compileall -q datamate scripts
python scripts/build_notebook.py
```

For the current Windows workspace only, the isolated dependency folder can be used by setting `PYTHONPATH` to `.deps` and the package's parent. Do not upload `.deps` as a Kaggle dependency bundle; its binary wheels target this Windows/Python environment.


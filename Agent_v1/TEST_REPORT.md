# Validation report

Recorded: 11 November 2026 (Asia/Bangkok).

## Version 1.0.4 range and public-split audit

The requested range is the 980 rows at positions 21–1000 in `Data/train/questions.jsonl`, across 30 businesses. A separate check used positions 21–50 within `biz_128` to match the Kaggle log selection. The independent check used all 1,000 questions from `Data/public_test`, across 14 businesses.

The local audit ran the production verified query compiler and SQL execution without Qwen. It used documented schemas, parallel business workers and an accelerated Excel reader for freshly prepared workbooks. Unsupported intents or missing source roles were classified without running SQL. Released training answers were used only to score the resulting predictions. The public split does not include released answers.

| Verified-only audit | Rows | Correct | Wrong | Abstained | Answered |
|---|---:|---:|---:|---:|---:|
| Split-wide training rows 21–1000 | 980 | 132 | 0 | 848 | 13.47% |
| `biz_128` rows 21–50 | 30 | 30 | 0 | 0 | 100% |
| Separate public test split | 1,000 | Unscored | Unscored | 986 | 1.40% |

Unsupported question intents accounted for 680 training and 920 public abstentions. Most remaining abstentions involved insufficient schema or value evidence, such as missing keys, ambiguous status labels or unresolved dates and amounts. The audits recorded no preparation errors or SQL execution failures for this release. Per-question predictions and abstention reasons are in `dist/audit/`.

The latest `biz_128` run matched 30 of 30 references. It read a balance of 43 Tiger beer cans from the snapshot documented for the requested date, recovered the customer ranking, matched Q1 2026 revenue of 2,174,852,500 and April 2025 cancelled orders of 31, and confirmed the 26 December 2025 price of 152,000 from the raw unit price, undiscounted line amounts and quantities.

The Kaggle traces showed literal date placeholders, reused SQL aliases, nonnumeric verification results, incorrect date interpretations, unsupported historical snapshots, and malformed or factually incorrect model reviews. The release grounds explicit periods against the question text, repairs temporal placeholders in parsed SQL, returns actionable repair feedback for invalid aliases and verification results, retries malformed reviewer output, and extends the evidence-backed route across documented exports. Printed subtotals stay available in raw data and are excluded from transaction queries.

Qwen inference, model-driven coverage and timing still require a Kaggle run with the checkpoint. This workspace had a 4 GB GPU and no checkpoint. The public audit can establish execution and route coverage only.

## Kaggle abstention fixes

- Version 1.0.3 passed 49 CPU tests and matched all 20 released answers in the `biz_128` development batch on the original workbooks. The local backend supplied empty mapping proposals and abstained if called for a generated plan. All 20 answers came from the verified query route. The full split and GPU fallback remain unmeasured.
- Version 1.0.2 passed 44 CPU tests. On the original `biz_128` workbooks, the verified totals route independently returned 89,526,400, 39,793,500, and 109,181,000. The local backend returned empty mapping proposals and rejected any request for a generated plan or review. All three answers matched the released references. The model-driven fallback still needs Kaggle evaluation.
- Version 1.0.1 passed 41 CPU tests. Using replayed mappings and SQL on the original `biz_128` workbooks, the three questions in the Cell 8 log returned 89,526,400, 39,793,500, and 109,181,000. All three matched the released references. This replay exercises normalization, duplicate handling, date coverage, and query execution. Kaggle model generation and timing still need verification.
- The Cell 8 follow-up recorded 40 CUDA out-of-memory failures across 20 questions. The updated Kaggle profile uses 512-token prefill chunks. All 39 CPU tests passed, including mocked long-prompt generation and a run interrupted by GPU memory exhaustion. Peak VRAM still needs a Kaggle T4 run.
- All 35 CPU tests passed with DuckDB 1.3.2 on SQLGlot 25.20.2 and 30.31.0.
- A replay of the first `biz_128` question with a test mapping and query recovered from the invalid deduplication proposal. The purchase workbook had 479 rows and zero amount parse errors. The pipeline returned 89,526,400, matching the released answer.
- All 11 code cells in the updated Kaggle notebook parsed as Python. Qwen inference coverage on Kaggle remains unverified.

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


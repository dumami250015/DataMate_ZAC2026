# DataMate Agent v1

An offline Qwen3.5-9B agent for Vietnamese questions over CSV, Excel, and SQLite business packages. It is implemented as a reusable Python package with a Kaggle notebook and a CLI.

**Start with `notebooks/01_datamate_runner.ipynb`.** Attach the source, challenge data, and a complete local Qwen checkpoint, edit the paths in Cell 1, and run a development subset before enabling the full run.

This is a working source implementation, not a measured competition score. CPU execution, ingestion, scoring, and controller behavior have been tested locally. The Qwen GPU loader, quantization kernels, and full inference accuracy must be tested in your Kaggle GPU session. See `TEST_REPORT.md` for the actual checks completed.

## What is implemented

| Component | Implementation |
|---|---|
| Input | CSV encoding/delimiter handling, all XLSX sheets, read-only SQLite tables |
| Profiling | Row counts, sampled rows, null counts, source headers and provenance |
| Schema interpretation | Qwen proposes typed mappings, grain, group filling, duplicate policy, coverage evidence |
| Normalization | Conservative numbers/dates, product-unit fields, group-scoped fill, exact duplicate marking |
| Storage | Bulk-loaded DuckDB, source-preserving columns, normalized semantic columns, reusable caches |
| Rules | Runtime split rulebook with stable clause references; complete rule text passed to planning/review |
| Capabilities | Structural prerequisites and explicit unknown state; parsing failures never establish absence |
| Planning | Validated JSON plans, parameterized calendar intervals, entity candidates |
| Common execution | Deterministic sum/count/distinct-count/average/ranking/group-sum compiler |
| General execution | Read-only SQL for debt, inventory, purchases, profit, industry-specific tables and unusual joins |
| Verification | AST restrictions, source-column checks, parse checks, explicit SQL invariant checks, semantic review |
| Repair | One bounded repair by default, with concrete failure feedback |
| Decisions | Typed answer, reviewed unanswerable, or abstain; no invented confidence percentages |
| Output | Decimal postprocessing, schema validation, per-question atomic commits, logs, resumable submission export |
| Evaluation | PA, coverage, attempted accuracy, per-type/business reporting, aliases, business-level folds |
| Deployment | Transformers backend, optional vLLM backend, Kaggle runner, Docker entry-point template |

The architecture's conceptual canonical tables are represented in v1 by **schema-family tables `t000`, `t001`, … with canonical semantic column names**. For example, a flat export can contain order and line fields together. This preserves original grain and accommodates new industries. The model joins those tables based on README and catalog evidence; the standard compiler performs the final aggregate. Specialized domain questions use the general SQL route rather than an incomplete fixed industry switch.

No answers or business-specific rules are hard-coded. IDs select data and name logs only. No trained embeddings, second LLM, external API, or generated Python execution is used. Fine-tuning is optional and is not needed to run this baseline; no training was performed for this implementation.

## Kaggle quick start

1. Create/upload a Kaggle dataset containing the extracted `Agent_v1` folder. `scripts/package_source.py` creates `dist/Agent_v1_source.zip` without local dependencies, caches, or weights.
2. Attach the challenge dataset with `train/` and/or `public_test/` intact. Each selected split needs `data/`, `rulebook.md`, `calendar.csv`, and `questions.jsonl`.
3. Attach a complete official `Qwen/Qwen3.5-9B` checkpoint: config, tokenizer/chat template, shard index, and **all** safetensors shards. Set MODEL to the actual checkpoint directory. Use a known revision and retain its license.
4. Select an available GPU. For a 16 GB T4-class GPU, begin with the NF4/FP16 candidate profile. For supported 32 GB+ devices, test unquantized BF16 separately. Hardware compatibility is checked by actually loading and generating; there is no silent CPU/random-weight fallback.
5. Import `notebooks/01_datamate_runner.ipynb` into Kaggle and edit SOURCE, SPLIT, MODEL, WHEELS, and WORK.
6. Install requirements from a compatible attached wheelhouse, or perform one Internet-enabled **setup-only** session with the notebook's explicit installation switches. Restart if necessary, then disable Internet and run inference offline.
7. Run the backend smoke test and the development business. Inspect `parse_errors`, logs, and training-only metrics.
8. Set `RUN_FULL=True` to generate every answer. Save `/kaggle/working/datamate/submission.jsonl` and the associated `runs/` artifacts.

Default example paths:

```text
/kaggle/input/datamate-source/Agent_v1
/kaggle/input/datamate-data/train
/kaggle/input/qwen35-9b-checkpoint
/kaggle/input/datamate-wheels
/kaggle/working/datamate
```

Kaggle dataset mounts can contain extra nested directories; use the paths shown in your notebook's input browser. The code never writes into `/kaggle/input`.

## Dependency setup and reproducibility

`requirements-cpu.txt` contains CPU dependencies. `requirements-kaggle.txt` adds the candidate GPU stack. These are compatibility ranges, **not a claim of a validated CUDA lockfile**. The Qwen3.5 model class and hybrid-attention kernels require a sufficiently recent compatible runtime.

In a setup session, starting with Kaggle's CUDA-enabled Torch installation:

```bash
python -m pip install -r /path/to/Agent_v1/requirements-kaggle.txt
```

After successful load/generation and regression tests:

```bash
python scripts/freeze_environment.py --output requirements-runtime.lock.txt
python -m pip download -r requirements-runtime.lock.txt --dest wheelhouse
```

Review the frozen environment: a full Kaggle environment can include unrelated packages or local URLs. Preserve the agent's actual transitive dependencies and the exact compatible Torch/CUDA build. Build/download Linux wheels using the same Python ABI and target platform, not this Windows development machine. Rehearse installation with networking disabled:

```bash
python -m pip install --no-index --find-links=wheelhouse -r requirements-runtime.lock.txt
```

Do not replace Torch solely because a newer version exists. If installation replaces an already imported package, restart the notebook. `scripts/audit_model.py MODEL --output model_inventory.json` inspects checkpoint tensor headers without loading the model; quantized storage counts must not be mistaken for original parameter counts.

## CLI usage

Run from the `Agent_v1` directory, or install it with `pip install -e .` in a prepared environment.

```bash
# Inspect hardware and installed packages.
python -m datamate.cli preflight --work /kaggle/working/datamate

# Load Qwen and verify structured generation without running the dataset.
python -m datamate.cli smoke --config configs/kaggle.yaml \
  --split /kaggle/input/datamate-data/train --model /actual/checkpoint

# Prepare selected business data, using automatic schema mapping.
python -m datamate.cli prepare --config configs/kaggle.yaml --business biz_101

# CPU inspection only: all raw columns preserved, no Qwen/schema interpretation.
python -m datamate.cli prepare --config configs/kaggle.yaml \
  --split /local/Data/train --work /local/Agent_v1/work --business biz_101 --raw-only

# Small inference run. --limit is applied AFTER --business selection.
python -m datamate.cli predict --config configs/kaggle.yaml --business biz_101 --limit 20

# Full inference; source, model and paths are configured or overridden.
python -m datamate.cli predict --config configs/kaggle.yaml \
  --split /kaggle/input/datamate-data/public_test --model /actual/checkpoint

# Score only selected development records; omit --selected-only for full-split PA.
python -m datamate.cli evaluate --predictions /work/submission.jsonl \
  --answers /data/train/answers.jsonl --questions /data/train/questions.jsonl \
  --selected-only --output /work/metrics.json

python -m datamate.cli folds --questions /data/train/questions.jsonl --output folds.json
python -m datamate.cli validate --submission /work/submission.jsonl --questions /data/public_test/questions.jsonl
```

`prepare --raw-only` is an ingestion diagnostic, not a substitute for the model-enabled agent. Ordinary `predict` automatically prepares uncached businesses. Reference answers are never supplied to Agent, the planner, the mapper, or the model prompts.

## Data representation

Each schema family exposes a view such as `t000`. Its physical table `t000_all` retains rows marked as export duplicates. The query worker permits only catalog views, so a generated query cannot accidentally include discarded duplicates.

Each view contains:

- `c_000`, `c_001`, …: original cell strings.
- Named typed columns such as `order_id`, `line_amount`, `quantity_base`, `paid_at`: validated mapping outputs.
- `_file`, `_sheet`, `_row`: source coordinates.
- `_parse_errors`: names of columns that failed parsing.
- `_duplicate`: duplicate marker.

Schema families group matching schemas from monthly sheets or numbered/year-suffixed file exports. A new schema/mapping produces a separate table. Repeated printed headers are removed only when they exactly match the source header. Other suspicious summary/footer rows remain visible so they must be explicitly excluded based on evidence.

Decimal separators are explicit column metadata. Ambiguous `189,000` with no established convention fails normalization instead of silently becoming 189 or 189000. Blank group values fill only under a mapped key and allowed fill columns. Identical rows in an orderless ledger remain distinct.

Capability `supported` means structural prerequisites exist, not that every query using them is correct. Time validity, joins, returns, and coverage still require checks. `unknown` is not `unanswerable`.

## Plan and query execution

`contracts.py` is the API definition. Plans use either:

- `route=metric`: one source SELECT implements business filters and emits normalized `value`, `entity_key`, and optionally `entity_name`. Python compiles the aggregate/ranking.
- `route=sql`: one or more SELECTs implement more general analytics.
- `route=unanswerable`: specific absence evidence, independently reviewed.
- `route=abstain`: unresolved interpretation.

Named parameters are resolved from the supplied as-of date: `$start`, `$end`, `$as_of`, `$as_of_end`, `$comparison_start`, `$comparison_end`. End dates are exclusive. Relative dates and Tết use deterministic calendar logic. A NULL unresolved bound is rejected if a query tries to use it.

For ratios, growth, differences, and average money, ask the model for separate exact scalar queries; Python Decimal performs the final calculation. The serializer handles half-up rounding and emits numeric JSON tokens. Do not compute a percentage by dividing integers in SQL and expect exact decimal semantics automatically.

All SQL is checked by sqlglot and executed in a fresh read-only DuckDB worker with external access and extension loading disabled, an allowlisted function set, memory/row caps, and a wall timeout. There is no arbitrary Python worker. This reduces the execution surface but is not a general-purpose hostile-code sandbox; source data remains read-only and the final Docker runtime should also disable networking.

The semantic review sees the supplied rules, question, source catalog, plan, and result preview. It can request one repair. Neither successful execution nor model agreement proves accuracy; the held-out evaluation is the measurement that matters.

## Reliability, logs, and resume

The runner writes `runs/<fingerprint>/results/<id>.json` atomically and records logs separately. It periodically exports both the run-specific submission and `WORK/submission.jsonl`. The final export includes explicit abstentions for unfinished questions.

Fingerprints cover source contents, code, checkpoint, backend settings, rulebook/calendar, question file, selected business packages, and stable environment properties. Full checkpoint hashing can take time and is included in startup. Transient free disk space is deliberately excluded. Changing semantics/configuration creates a new run. Restore the entire saved work directory for cross-session resume.

An interrupted question is not trusted unless its result was committed. Deadline interruption does not mark unfinished questions completed. Completed abstentions, including genuine failed attempts, are retained on resume; change the run configuration or disable resume to retry those deliberately.

Per-question logs include plan attempts, errors, executed SQL, parameters, checks, result previews/full small query results, semantic review, final record, elapsed time, and available generation statistics. See `preparation_error` for business-level failures and `unexpected_error` for programming/backend failures. Use `runtime.fail_fast=true` while developing.

## Evaluation

The scorer implements the published type/format rules, string normalization without removing accents, per-item list aliases, per-row table aliases, ordered lists, and table row-order independence. Numeric matching uses an explicit absolute epsilon of 1e-6 to accommodate floating serialization; the organizer's exact tolerance is not published, so local scoring is an approximation.

Use business-held-out splits, not random individual questions. The supplied `folds` command creates deterministic business folds; inspect/balance industries and schema families for serious experiments. Keep calibration and final comparison businesses separate. This baseline uses hard checks and semantic review, not a fitted probability calibrator.

Run tests:

```bash
python -m pip install -e '.[test]'
python -m pytest -q
```

Tests use temporary synthetic data and a fake model where appropriate. They do not demonstrate Qwen accuracy. The model has not been fine-tuned on public/private data, and no public answers have been collected.

## Troubleshooting

| Symptom | Action |
|---|---|
| Missing Qwen3.5 model class | Install a compatible recent Transformers build, restart, rerun smoke test |
| Missing language weights or shape mismatch | Stop; check complete checkpoint and loader compatibility |
| NF4 or DeltaNet kernel error | Inspect GPU/Torch/CUDA/bitsandbytes compatibility; select a tested profile explicitly |
| Out of memory | Reduce context only after measuring prompt lengths, reduce model/batch overhead, or change supported hardware; do not silently truncate mandatory rules |
| Context budget exceeded | Inspect catalog/README size; increase a tested context budget or reduce profile samples/entity candidates; full rules are never silently removed |
| Unsupported SQL function | Inspect its sqlglot-normalized name; add only a reviewed pure function and a test |
| Parse failure in a required column | Inspect raw cells and mapping; repair the mapping/normalizer, invalidate the code cache, rerun |
| Almost every answer abstains | Inspect preparation and first question logs; this often signals environment, mapping, context, or SQL issues |
| Slow preparation | Ensure bulk inserts are active, inspect spreadsheet size and model mapping calls, reuse matching caches |
| Notebook closes | Restore saved work outputs and rerun identical inputs/configuration |

## Competition Docker handoff

`predict.sh` reads `/data`, writes `/result/submission.jsonl`, and retains run logs under `/result/runs`. The organizer requests `/result/logs`; the runner also publishes per-question logs there. Use the organizer's approved CUDA/PyTorch base image and make a target-platform wheelhouse and lockfile before building `Dockerfile`.

Place the local checkpoint at `/opt/models/qwen35-9b` in the final image (or pass `--model`). The provided Dockerfile is a build template requiring `BASE_IMAGE`, `wheelhouse/`, and `requirements-runtime.lock.txt`; it does not pretend that an untested CUDA image is ready for submission. Include the checkpoint in the build explicitly as documented below.

```dockerfile
# Add this to your final image build, with the allowed checkpoint in the context:
COPY models/qwen35-9b /opt/models/qwen35-9b
```

Rehearse with read-only input, networking disabled, cold caches, and a wall deadline. The default Docker profile differs in precision from the Kaggle NF4 profile; compare predictions and freeze a configuration that meets the competition's reproducibility requirement. Kaggle performance does not establish six-hour feasibility on the organizer's hardware.

## Reference documentation

- [Qwen3.5 model/loader documentation](https://huggingface.co/docs/transformers/model_doc/qwen3_5)
- [Qwen3.5-9B model card](https://huggingface.co/Qwen/Qwen3.5-9B)
- [Transformers bitsandbytes integration](https://huggingface.co/docs/transformers/quantization/bitsandbytes)
- [Kaggle notebooks](https://www.kaggle.com/docs/notebooks)
- Competition semantics: the mounted split's `rulebook.md`, `calendar.csv`, business READMEs, and the provided overview.


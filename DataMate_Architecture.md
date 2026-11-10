# DataMate agent: implementation architecture for Kaggle

Design date: 10 November 2026. Primary model: Qwen/Qwen3.5-9B.

This document is an implementation specification, not an implemented or benchmarked agent. Configuration values below are initial experimental settings. Backend compatibility, memory use, accuracy, and runtime must be measured on the selected Kaggle accelerator and again on the organizer's hardware.

**1. The system we are building**

DataMate receives a business data package, a Vietnamese question, an as-of date, a split-specific rulebook, and a solar–lunar calendar. It returns exactly one typed JSON answer, an evidence-based `unanswerable`, or an `abstain` when the agent cannot establish a sufficiently reliable answer.

The central design decision is to separate understanding from calculation:

- Qwen3.5-9B interprets unfamiliar schemas, language, entities, and rules, then proposes structured execution plans.
- Python and SQL parse files, resolve dates, perform joins, compute values, check invariants, and serialize answers.
- A controller limits retries, caches reusable work, records evidence, and decides whether to answer.

The notebook is an entry point and inspection interface. Business logic belongs in a Python package that runs unchanged in Kaggle and the final offline container.

Our training inventory contains 30 businesses and 3,000 questions: 107 XLSX files, 20 CSV files, and one SQLite database. Numeric and percentage answers together account for 82.1% of training questions. Prioritize robust arithmetic queries while keeping a generic route for unfamiliar private-test industries. Public-test structure contains 14 businesses and 1,000 questions; it must not supply training examples or reference answers.

Success is measured by Penalized Accuracy, `(correct - wrong) / all_questions`, rather than coverage alone. The final solution must also satisfy the 15B total inference-parameter cap, offline operation, automatic processing, six-hour reference-hardware limit, and reproducibility requirements in Overview/Overview.md.

**2. Architecture at a glance**

```text
Kaggle notebook / command-line entry point
                    |
                    v
        Run configuration + hardware preflight
                    |
           load one local Qwen model
                    |
          +---------+----------------------+
          |                                |
          v                                v
 Split rulebook + calendar        Business files + README
          |                                |
   Rule registry                  Readers and profilers
          |                                |
          |                      Schema mapper + validator
          |                                |
          |                    Canonical tables / raw tables
          |                                |
          +-------------+------------------+
                        |
             BusinessContext cache
       schema + entities + capabilities + DB
                        |
          Question + as_of_date
                        |
          Retrieve relevant context
                        |
              Structured planner
                        |
           Resolve dates/entities
                        |
       Validate plan and answerability
                        |
          Compile / bounded SQL fallback
                        |
                 Execute query
                        |
          Verify result and provenance
                        |
        Optional targeted repair (bounded)
                        |
       Answer / unanswerable / abstain
                        |
        Atomic result + audit log + export
```

There are two execution scopes:

| Scope | Work | Reuse |
|---|---|---|
| Once per split | Read rulebook, load calendar, build validated rules | Every business and question in that split |
| Once per business | Profile files, map schemas, normalize, discover capabilities, index entities | Every question for that business |
| Once per question | Interpret, plan, execute, verify, serialize | Cache only with complete semantic inputs |

The system remains a controlled workflow, rather than an unrestricted agent loop. One model performs different roles through distinct prompts; separate model instances are not needed for separate roles.

**3. Kaggle workspace and artifact layout**

Attach the source code, business dataset, model checkpoint, and dependency wheels as separate versioned inputs. Names below are examples; actual attached paths must be configured explicitly.

```text
/kaggle/input/
  datamate-data/                  # train/, public_test/
  datamate-source/                # datamate/ package and configs/
  qwen35-9b-checkpoint/           # full local checkpoint and tokenizer
  datamate-wheels/                # tested offline installation wheels

/kaggle/working/datamate/
  environment.json
  cache/
    <semantic_fingerprint>/
      rules.json
      businesses/<business_id>/
        source_manifest.json
        catalog.json
        mapping.json
        capabilities.json
        entities.json
        business.duckdb
  runs/<run_id>/
    config.json
    manifest.json
    results/<question_id>.json
    logs/<question_id>.json
    progress.json
    submission.jsonl
    metrics.json                 # validation runs only
    errors.csv                   # validation runs only
```

Treat `/kaggle/input` as read-only. Store caches and outputs under `/kaggle/working`. Save/export important outputs before the notebook session ends; do not assume unsaved local session state survives a restart. Check current notebook quotas and available accelerators in the Kaggle UI instead of building fixed session durations into the program. See [Kaggle notebook documentation](https://www.kaggle.com/docs/notebooks).

Use three operational modes:

| Mode | Purpose | Reads reference answers? |
|---|---|---|
| `prepare` | Build data catalogs, mappings, and caches | No |
| `predict` | Produce answers from mounted split | No |
| `evaluate` | Compare completed training-split predictions with labels | Yes, in a separate evaluator |

For a new split, `predict` automatically performs missing preparation. A cached Kaggle run must not hide the cold-start cost of the final Docker run.

**4. Runtime profiles and model deployment**

Use the post-trained `Qwen/Qwen3.5-9B` checkpoint. Pin a checkpoint revision; never silently download a moving `main` revision during inference. Its release predates the competition cutoff. The complete deployed parameter inventory must include every retained component and any auxiliary models, not just the advertised language-model size. [Official model card](https://huggingface.co/Qwen/Qwen3.5-9B), [official release history](https://github.com/QwenLM/Qwen3.8#news).

Qwen is used only with text for this task. XLSX and SQLite should be parsed directly, not rendered as images. The Transformers documentation provides a text-only class and a multimodal class; use the checkpoint-loading route supported by the pinned library and verify that all expected language weights load. Unexpected missing or randomly initialized language weights are a startup failure. Do not manually rename checkpoint keys without a validated conversion. [Transformers Qwen3.5 documentation](https://huggingface.co/docs/transformers/main/model_doc/qwen3_5).

Recommended profiles, subject to smoke tests:

| Hardware detected | Starting experiment | Important qualification |
|---|---|---|
| One T4-class 16 GB GPU | Transformers, validated 4-bit load, FP16 compute, batch 1, short context | Test Qwen3.5 linear-attention kernels and quantization together |
| Two T4-class 16 GB GPUs | Start with one-GPU quantized profile; benchmark sharding only if needed | Two devices do not form one transparent 32 GB allocation |
| P100-class GPU | Compatibility experiment only, FP16 and supported fallback operations | Do not assume modern fused kernels work; change accelerator if smoke test fails |
| One GPU with at least 32 GB | BF16 where supported, moderate context and batch | Weight memory is only part of total memory |
| Organizer's two RTX 5090 GPUs | Tested Blackwell-compatible stack; tune serving after correctness | Include cold preparation and model load in six-hour timing |

These are candidates, not guarantees about which Kaggle GPUs are available to your account. NVIDIA's Kaggle guide shows accelerator selection and example T4/P100 options. [NVIDIA Kaggle guide](https://docs.nvidia.com/datascience/deployment/latest/platforms/kaggle/).

For quantization, use a validated bitsandbytes NF4 configuration as the first memory-saving experiment. FP16 is the starting compute dtype on T4; choose BF16 only when the hardware/backend support it. Quantized weight size is not peak VRAM, and some layers/state remain at higher precision. [Transformers bitsandbytes documentation](https://huggingface.co/docs/transformers/quantization/bitsandbytes).

Keep two backend adapters behind one interface:

```python
class LLMBackend:
    def generate(self, messages, *, schema, thinking,
                 max_new_tokens, request_key) -> "GenerationResult": ...
```

- `TransformersBackend`: initial Kaggle implementation, small batches, easy introspection.
- `VLLMBackend`: optional throughput implementation after validating model, GPU, dtype, and kernel support.

Do not copy installation commands for much larger Qwen MoE models and assume they establish 9B compatibility. Resolve a working package set experimentally, then pin it in `requirements-kaggle.lock.txt` and a separate `requirements-docker.lock.txt`. Match Python, PyTorch, CUDA, and compiled wheels. A major PyTorch replacement may require restarting the notebook before importing it.

Thinking should be off for ordinary planning and selectively enabled for difficult interpretation or repair. Use the official `enable_thinking` chat-template control, not `/think` or `/nothink`. Bound generation and validate the final structured content separately from reasoning text. [Model inference instructions](https://huggingface.co/Qwen/Qwen3.5-9B#instruct-or-non-thinking-mode).

Initial experimental budgets: 8,192 total context tokens; 768 new tokens for simple plans; 2,048 for complex plans or repair; at most one semantic repair. Token budgets include any reasoning tokens. Reject a truncated plan rather than parsing a plausible prefix. Increase these only when measured failures justify it.

**5. Package structure and ownership**

```text
datamate/
  cli.py                       # prepare, predict, evaluate
  config.py                    # validated config and resolved paths
  contracts.py                 # typed records and JSON schemas
  runtime.py                   # fingerprint, deadline, resource tracking
  pipeline.py                  # orchestration, not business formulas
  io/
    discovery.py               # file inventory and fingerprints
    csv_reader.py
    excel_reader.py
    sqlite_reader.py
    profile.py                 # grain, nulls, distinct values, date spans
  semantic/
    schema_mapper.py           # source-to-canonical proposals
    mapping_validator.py
    normalize.py               # dates, numbers, grouped cells
    entities.py                # codes, aliases, retrieval
    capabilities.py            # supported, absent, unknown
    rulebook.py                # rule extraction and dependencies
    calendar.py                # deterministic period resolution
  storage/
    database.py                # raw and canonical DuckDB tables
    cache.py                   # validated, fingerprinted cache writes
  planning/
    retrieve.py                # rules, schemas, entities, train plans
    planner.py
    validate.py
    compiler.py                # typed plan -> parameterized SQL
  metrics/
    sales.py
    customers.py
    debt.py
    inventory.py
    purchases.py
    profit.py
    industry.py                # extension mechanism, not business-ID cases
  execution/
    sql.py
    transforms.py              # approved deterministic transforms
    verifier.py
    repair.py
  llm/
    base.py
    transformers_backend.py
    vllm_backend.py
    prompts/
  output/
    decision.py
    serialize.py
    journal.py
  evaluation/
    splits.py
    scorer.py
    reports.py
    synthetic.py
configs/
notebooks/01_datamate_runner.ipynb
tests/
predict.sh
```

The notebook should not contain copies of these implementations. Import the package or invoke its CLI. A future schema or metric fix should require one code change, not edits to several notebook cells.

**6. Core contracts**

Define contracts with Pydantic or dataclasses plus explicit validation. They are the boundaries between modules and the basis for tests.

| Contract | Required contents |
|---|---|
| `Question` | id, business_id, raw question, as_of_date |
| `SourceManifest` | file paths, hashes, sizes, sheets/tables, reader versions |
| `TableProfile` | source columns, types, grain hypothesis, samples, nulls, key candidates, coverage |
| `SchemaMapping` | canonical fields, source fields, transformations, evidence, unresolved mappings |
| `RuleDefinition` | stable semantic name, source clause, structured meaning, scope, dependencies, validation status |
| `Capability` | supported/absent/unknown, required fields, evidence, date validity, limitations |
| `BusinessContext` | catalog, mappings, capabilities, entity index, database handle, rule fingerprint |
| `QueryPlan` | operation, metric, entities, time specification, population, grouping, ordering, output |
| `ExecutionResult` | exact values, result schema, diagnostics, source/SQL provenance |
| `VerificationReport` | passed/failed/unknown checks with evidence and severity |
| `Decision` | answer/unanswerable/abstain, internal rationale, calibrated reliability if available |
| `SubmissionRecord` | id, type, answer or optional reason, exact competition format |

Do not place training labels in `BusinessContext`. Prediction code should not receive an answers-file path.

**7. File discovery, profiling, and normalization**

Read all files belonging to the requested business, including all workbook sheets and split exports. Preserve original bytes/values and provenance before normalization.

For CSV, detect encoding and delimiter conservatively; retain raw string forms of identifiers. For XLSX, use streaming reads where possible, inspect title/header rows, distinguish repeated headers and totals, and handle sheets whose dimensions are unavailable. Reading cached formula values does not calculate formulas: missing caches are unresolved data, not zero. SQLite must be opened read-only; import needed tables using explicit column metadata.

Profile representative nonempty rows across a file, not only its first few rows. Include unusual formatting and null patterns. Provide the model a compact catalog, README excerpts, and samples. Execute deterministic column transformations after the model proposes a mapping.

Normalization order:

1. Identify table sections, row grain, and headers.
2. Preserve source identities and raw values.
3. Identify reliable group/order boundaries.
4. Fill only permitted group-level blanks within a verified group.
5. Parse identifiers, dates, numeric strings, and units.
6. Resolve statuses using explicit columns, documented notes, and return sources.
7. Apply the rulebook's duplicate policy at the correct grain.
8. Map entities, validate joins, and construct canonical tables.
9. Record coverage, capabilities, and unresolved errors.

Never fill blank customer cells across unrelated orders. Never deduplicate an orderless ledger merely because two rows look identical. Never infer order count from line count unless source semantics establish one row per order/visit.

Numeric parsing must distinguish `25.000đ`, `1,500.0000`, `189,000`, and quantities such as `1 kg`. Use column-level format evidence and expected units; do not strip all punctuation indiscriminately. Preserve ambiguous parses as unresolved. Use Decimal constructed from strings, not binary floats, for monetary transformations.

Scope identifiers to the business and, when justified by source semantics, branch or source system. An order spanning multiple sheets must retain the same identity; different orders sharing a local code must not be merged accidentally. Joining two sources requires evidence of their key scope.

Keep provenance such as file, sheet/table, row index, original key, normalization actions, and duplicate-source references. A correct-looking answer should be traceable to its supporting data.

**8. Canonical data and capability model**

Use a persistent analytical database per business. Suggested tables:

| Table | Grain and key fields |
|---|---|
| `orders` | order_key; placed_at, status, customer_key, payment_method, channel, total_payable |
| `sales_lines` | line_key; order_key, product_key, quantity_selling, selling_unit, quantity_base, line_amount, unit_cost_selling |
| `products` | product_key; source_code, source_name, category, base_unit, catalog_unit_cost |
| `customers` | customer_key; source_code, source_name, group and recorded attributes |
| `payments` | payment_key; order_key, paid_at, amount, event_kind |
| `purchase_headers` | receipt_key; received_at, supplier_key, status, receipt_amount |
| `purchase_lines` | receipt_line_key; receipt_key, product_key, quantity, unit, unit_cost |
| `stock_events` | stock_event_key; product_key, branch_key, occurred_at, delta_base, balance_after, source_order |
| `stock_snapshots` | product_key, branch_key, snapshot_at, quantity_base |
| `lots` | lot_key; product_key, received_at, expires_at, purchase_cost_base |
| `lot_allocations` | event_key, line_key, lot_key, movement_quantity, occurred_at |
| `expenses` | expense_key; date/month, amount, category |
| `price_history` | product_key; effective_at, price_base |
| `unit_conversions` | product_key, unit, factor_to_base |

Retain `raw_*` tables and optional industry tables such as service visits, warranties, deliveries, and installment schedules. Do not force a repair visit into a sales-line grain when it would lose meaning.

Canonical field presence does not imply semantic availability. Record three capability states:

- `supported`: necessary data and interpretation are established.
- `absent`: the released package demonstrably lacks the necessary information.
- `unknown`: parsing, mapping, completeness, or interpretation is unresolved.

Example:

```json
{
  "metric": "stock_at_date",
  "state": "supported",
  "method": "snapshot",
  "valid_at": "2026-06-24",
  "historical_reconstruction": "unknown",
  "evidence": ["README: snapshot is end-of-final-day stock"]
}
```

Capabilities are evaluated for a particular question. A final-day snapshot supports that date, but does not automatically support earlier dates. For coverage, distinguish documented business coverage from observed transaction dates: a day without orders can still be inside covered data. Maintain source-specific coverage for payments, sales, and stock as well as business-level coverage; do not infer all coverage from one table's min/max timestamps.

**9. Rulebook engine**

Load the supplied rulebook every split. Cache by its content hash. Define stable semantic primitives such as revenue, base quantity, cancellation rate, debt, and stock-at-time; populate their current policies from the split's rules.

Each extracted rule retains a source clause and validation state. Extraction is a proposal, not proof. Check required fields, incompatible populations, date policies, and dependencies. Unknown rules remain available to the planner and generic route; do not silently substitute training defaults.

Interpretation order: explicit question instruction; applicable industry override; general split default. README statements explain source meanings and availability. If apparent conflicts cannot be resolved from the supplied material, mark the affected interpretation unresolved rather than inventing a universal precedence rule.

Example definition:

```json
{
  "name": "revenue",
  "source_measure": "sales_lines.line_amount",
  "time_basis": "orders.placed_at",
  "population": "exclude_cancelled_and_entire_returned_orders",
  "deduct_order_discount": false,
  "include_delivery": false,
  "include_vat": false,
  "source_clause": "section_1",
  "status": "validated"
}
```

Build separate populations for revenue, debt, cancellation-rate denominator, selling-price observations, inventory movements, and industry visits. There is no universal `valid_order` policy for every metric.

If a split changes revenue eligibility, a compiler predicate or supported policy must change too. Merely mentioning the new rule in a prompt while retaining an old SQL template is insufficient. Keep rule-dependent materializations keyed by rule hash and rebuild them when needed.

**10. Dates and entity resolution**

The model emits a symbolic time intent; deterministic code resolves it. Every resolved interval stores its originating expression and policy.

Example with as-of date 2026-03-04:

| Expression | Inclusive business dates |
|---|---|
| hôm nay | 2026-03-04 |
| tuần rồi | 2026-02-23 to 2026-03-01 |
| tháng này | 2026-03-01 to 2026-03-04 |
| comparison with previous month | 2026-02-01 to 2026-02-04 |
| 7 ngày gần nhất | 2026-02-26 to 2026-03-04 |

Use half-open timestamp ranges internally. Resolve lunar periods using the supplied calendar. Distinguish explicit periods, relative periods, and point-in-time balances. Do not globally remove records after as_of_date: later return status can affect revenue for an earlier order under the supplied rules.

Entity indexes preserve codes, original names, observed aliases, normalized forms, and attributes such as size or concentration. Retrieval order is exact code, exact alias, conservative normalization, then fuzzy/accent-insensitive candidate retrieval. The model selects among candidates using context. Ambiguous candidates with different results require repair or abstention.

Different customer codes remain distinct even if names match. Similar active ingredients or categories must not be merged when exact matching is required. Accent-insensitive strings are search aids, not final output replacements. Render names from source records.

**11. Planning and prompt contracts**

Use role-specific prompts for mapping, rule extraction, planning, and repair. Each requests a compact structured artifact and supporting references, not an unrestricted narrative.

Planner input contains the raw question, as-of date, relevant rules, schema/grain descriptions, capabilities, resolved entity candidates, and optionally two to four training-only plan examples. Include dependent rules: revenue eligibility, unit conversion, ordering, and formatting may all be needed for one ranking query. Do not truncate away mandatory policies to fit a context budget.

Example plan before compilation:

```json
{
  "version": 1,
  "operation": "rank",
  "metric": "quantity_base",
  "entity": "product",
  "entity_filters": [],
  "time": {"kind": "previous_week", "basis": "order_date"},
  "population": "revenue_eligible",
  "exclude_non_product_lines": true,
  "group_by": ["product_key"],
  "sort": [
    {"field": "metric_value", "direction": "desc"},
    {"field": "source_code", "direction": "asc"}
  ],
  "limit": 1,
  "output": {"type": "string", "value": "source_name"},
  "required_capabilities": ["sales_quantity", "product_identity"],
  "rule_refs": ["sales_population", "best_seller", "unit_conversion", "ranking"]
}
```

Validators reject unknown identifiers, unsupported operations, illegal joins, incompatible output types, unresolved entities, missing dependencies, and source fields that do not exist. The compiler fills exact dates and approved predicates. Limit and filters are parameters, not concatenated SQL fragments from user text.

Keep the plan language compositional: aggregate, filter, distinct count, rank, compare, ratio, balance-at-time, ordered list, and table projection. Permit several measures and periods so questions asking for multiple numbers can produce a list in question order.

Three routes:

1. Validated metric plan compiled to tested SQL: default route.
2. Generated read-only SQL over known tables: unfamiliar but expressible questions.
3. Specialized approved transformation or restricted worker: composite cells and event reconstruction not supported by the first two routes.

Delay unrestricted generated Python until needed. Prefer adding reusable transforms with explicit contracts. Neither arbitrary SQL nor Python executes in the notebook's main process with unrestricted file access.

**12. Deterministic execution and metric definitions**

Use DuckDB for joins and aggregations; Python Decimal for exact final divisions and rounding. DuckDB documents that decimal division produces approximate floating-point results, so exact input columns alone do not guarantee exact percentages. [DuckDB numeric documentation](https://duckdb.org/docs/current/sql/data_types/numeric).

Preserve exact values internally. Apply the current rulebook's rounding only at the specified stage. Where a ranking criterion is an average or ratio, retain its unrounded value unless the governing rule defines a rounded criterion. Do not accidentally create ranking ties through early formatting.

| Metric | Implementation policy |
|---|---|
| Revenue | Sum recorded discounted line amounts over the specified eligible orders |
| Order count | Count distinct verified order identities, not lines |
| Base quantity | Use recorded base quantity or validated product/unit conversion exactly once |
| Gross profit | Revenue minus selling quantity times recorded cost per selling unit |
| Net profit | Gross profit minus applicable recorded expenses |
| Debt at D | Eligible order payable less payments through D; retain positive residual per order |
| Cancellation rate | Cancelled orders / all orders in period, using specified date basis |
| Growth | 100 * (current - previous) / previous, signed, with explicit zero-denominator handling |
| Unsold products | Previously sold eligible products with no qualifying sale in requested period |
| Stock value | Positive stock at requested time times the specified catalog unit cost |
| Expiring stock | Lot-level remaining quantities and lot-specific cost under pharmacy rules |

Aggregate each many-side relation to the intended join grain before joining. For debt, aggregate payments by order before joining to order headers. Do not join raw payments and raw lines and then sum either amount.

Revenue excludes entire returned orders where required, including when return evidence is recorded later. Debt may still include returned orders. Physical stock uses actual movements and return allocations; revenue exclusions are not physical inventory movements.

For stock, prefer a valid ledger balance at the requested time, then a complete opening-balance/movement reconstruction, then a snapshot at the exact applicable time. Reversing from a later snapshot requires complete intervening events. Reject incomplete reconstructions. Equal-timestamp events need a documented or validated sequence key; otherwise flag ambiguous ordering.

If a required denominator is zero and the supplied rules do not define the result, keep this as an unresolved semantic case. Do not automatically output zero or unanswerable. Resolve from authorized training evidence/rules or abstain.

Generated SQL is checked using an AST parser, limited to approved read-only operations and relations, and executed with a timeout. Reject external file reads, extension loading, attachments, writes, and arbitrary user-defined functions in generated SQL. Parsing alone is not a security boundary; execute workers with resource and filesystem constraints. Preparation code may write approved cache tables, but the query worker cannot mutate source data.

**13. Verification, repair, and decision policy**

Run cheap checks on every candidate and expensive checks only for relevant risk.

| Layer | Checks |
|---|---|
| Plan | Rule dependencies, entities, periods, metric and output type |
| Data | Capability status, coverage, parsing errors in required fields |
| Relational | Unique dimension keys, join multiplicity, unmatched keys |
| Arithmetic | Units, denominator, aggregation grain, rounding |
| Result | Requested shape, sorted order, entity identity, finite numeric values |
| Cross-check | Independent aggregate expression or targeted recomputation where justified |

Do not apply unsupported invariants. A warranty ratio can exceed 100% when warranties concern earlier sales; category totals can omit ungrouped records unless the rulebook defines an exhaustive partition.

Repair input includes the original plan, failed check, minimal relevant schema/rules, and execution diagnostics. It must return a revised plan or a structured unresolved outcome. Revalidate and reexecute. A later generation agreeing with the first is weak evidence because both share the same model.

Decision table:

| Evidence | Decision |
|---|---|
| Required information demonstrably absent | Candidate `unanswerable`, subject to reliability policy |
| Source cannot be parsed or mapping is unresolved | `abstain` |
| Covered period contains no qualifying events | Return the metric's supported empty result, often zero |
| Requested period entirely outside coverage | `unanswerable` under supplied rulebook |
| Partial overlap with coverage | Apply the supplied partial-coverage policy |
| Multiple plausible interpretations produce different answers | Repair, then abstain if unresolved |
| Validated supported result with sufficient estimated reliability | Return typed answer |

For an exact candidate answer with correctness probability p, expected score is 2p - 1. The ideal answer threshold is p > 0.5, but this requires calibration. Do not interpret a hand-written check score or the LLM's confidence as a probability.

Start with hard failure gates plus conservative, interpretable acceptance rules. Calibrate later using held-out businesses, considering mapping validity, entity ambiguity, missing dependencies, repair use, and verification outcomes. Calibrate `unanswerable` too; it receives a wrong-answer penalty when incorrect. Keep the calibration set distinct from the final evaluation set.

**14. Output, logging, resume, and deterministic ordering**

Only the serializer creates competition output. It emits JSON numbers, percent values in percent units, ISO dates, booleans, source names/codes, ordered lists, and tables in requested column order. No prose or Markdown surrounds the answer. Use `allow_nan=False`; validate after serialization. Ensure Decimal values become JSON numeric tokens with the intended value, not quoted strings.

Store each completed record atomically in `results/<id>.json` using write-to-temporary-file then replace. Update progress through one writer. Rebuild `submission.jsonl` periodically from these records in original question order, using atomic replacement. This guarantees the submission contains completed answers even if the run is interrupted; missing questions score as abstentions. Final export can explicitly emit abstain records for all missing IDs.

On resume, validate the complete run fingerprint and load only valid, completed per-question results. A crashed worker's partial temporary file is ignored. Do not reuse output when data, rules, model, prompts, code, or decoding settings changed.

Fingerprints include source contents, rulebook/calendar hashes, mapping/normalizer versions, model checkpoint, quantization, tokenizer/template, prompt version, code version, backend, and relevant generation settings. Business/question IDs are routing and bookkeeping keys only; never branch to special answers or policies by ID.

Log question ID, final answer, elapsed time, plan, resolved entities/dates, rule references, executed query, row-count diagnostics, validation results, error category, and fingerprints. Avoid copying full data tables into each log.

Set deterministic ordering and fixed seeds. Start with a tested deterministic decoding profile for non-thinking planning; evaluate any sampling profile across repeated runs. Seeds do not guarantee equivalence across GPU architectures or quantization. Kaggle and Docker configurations must be compared explicitly, especially if precision differs.

**15. Notebook execution: cell-by-cell contract**

The first notebook should contain the following cells. CLI/module calls shown here describe the proposed interface; they become runnable once the package is implemented.

| Cell | Action | Visible success evidence |
|---|---|---|
| 1 | Resolve attached dataset, source, checkpoint, wheel paths | Printed paths and required-file checks |
| 2 | Inspect Python, RAM, disk, GPU names/memory/capability, driver, Torch/CUDA | Saved environment.json |
| 3 | Install pinned missing dependencies from local wheelhouse; restart if needed | Import checks and exact versions |
| 4 | Enable offline flags, construct config, validate split structure | Resolved configuration and fingerprint |
| 5 | Load model once and run backend smoke tests | Structured generation, thinking toggle, peak memory, timing |
| 6 | Load questions and select businesses for development or full inference | Counts; no labels passed to predictor |
| 7 | Prepare split rules/calendar and selected business contexts | Catalog/mapping/capability summaries |
| 8 | Run an end-to-end development subset | Answers and failure taxonomy |
| 9 | Evaluate training predictions in separate evaluator | PA, coverage, type/operation breakdown |
| 10 | Run or resume full prediction | Progress, ETA, incremental submission |
| 11 | Validate export and package logs/config | Unique IDs, complete schema checks, output paths |

Illustrative configuration construction:

```python
from pathlib import Path
import os

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"

# Replace these with the actual attached paths.
split_dir = Path("/kaggle/input/datamate-data/train")
model_dir = Path("/kaggle/input/qwen35-9b-checkpoint")
work_dir = Path("/kaggle/working/datamate")
work_dir.mkdir(parents=True, exist_ok=True)

assert (split_dir / "questions.jsonl").exists()
assert (split_dir / "rulebook.md").exists()
assert (split_dir / "calendar.csv").exists()
assert (model_dir / "config.json").exists()
```

Hardware preflight should use `torch.cuda.is_available()`, device properties, `torch.cuda.get_device_capability`, and available memory, plus `nvidia-smi` if installed. Read available CPU RAM and free disk. A model load must be attempted only after selecting a profile that passed compatibility checks. CPU-only runs can validate ingestion and scoring but are not the target for full inference.

Do not use `pip install -U` in a reproducible run. Build a compatible wheelhouse in a separate preparation session, save the exact environment, then test installing with `--no-index --find-links` in an offline notebook. Model/tokenizer loads must use local paths and local-only loading. Importing an inference backend must not trigger downloads.

**16. Configuration contract**

Example, with values to tune on development data:

```yaml
schema_version: 1
mode: predict
paths:
  split: /kaggle/input/datamate-data/train
  model: /kaggle/input/qwen35-9b-checkpoint
  work: /kaggle/working/datamate
model:
  name: Qwen/Qwen3.5-9B
  revision: REQUIRED_PINNED_REVISION
  backend: transformers
  profile: kaggle_t4_4bit_candidate
  local_files_only: true
  context_tokens: 8192
  simple_max_new_tokens: 768
  complex_max_new_tokens: 2048
  simple_thinking: false
  complex_thinking: true
  batch_size: 1
pipeline:
  semantic_repairs: 1
  generated_sql_fallback: true
  generated_python_fallback: false
  example_retrieval: training_partition_only
  strict_provenance: true
runtime:
  seed: 42
  sql_timeout_seconds: 15
  run_deadline_seconds: 18000
  finalization_reserve_seconds: 300
  export_every_questions: 10
  resume: true
```

The five-hour deadline is an initial internal target below the organizer's six-hour limit; adapt it to the actual Kaggle session budget. The deadline starts at entry-point startup, before model loading or preparation. Per-query timeout is a starting safeguard, not a claim about observed execution latency.

Keep quantization/dtype settings in the selected profile. Do not silently switch precision after an out-of-memory error. First reduce batch size or context within validated limits; if a profile change is necessary, use a new run fingerprint and compare outputs. Detect output truncation caused by reduced budgets.

**17. Controller pseudocode**

```python
def predict(config):
    runtime = start_deadline_and_manifest(config)
    questions = read_questions(config.paths.split)
    journal = open_compatible_journal(config, runtime)
    model = load_local_backend(config.model)
    smoke_test(model)
    rules, calendar = prepare_split(config, model)

    for business_id, batch in group_by_business(questions):
        if runtime.must_finalize():
            break
        try:
            context = prepare_business(business_id, config, rules, model)
        except PreparationFailure as error:
            journal.record_business_failure(batch, error)
            continue

        for question in batch:
            if journal.has_valid_result(question.id):
                continue
            if runtime.must_finalize():
                break
            try:
                evidence = retrieve_context(question, context, rules)
                plan = model_plan(question, evidence, model)
                plan = resolve_and_validate(plan, question, context, calendar)
                candidate = execute_or_classify_missing(plan, context)
                report = verify(candidate, plan, context)
                if should_repair(report, runtime, config):
                    plan, candidate, report = bounded_repair(
                        question, plan, candidate, report, context, model)
                decision = choose_output(candidate, report, context)
            except RecoverableQuestionFailure as error:
                decision = abstain_with_internal_error(error)
            journal.commit(question, decision)
            journal.export_if_due(questions)

    journal.export_submission(questions, missing_as_abstain=True)
```

Programming defects should be surfaced in development reports, not silently swallowed as ordinary unanswerability. Production may continue after isolated recoverable errors, but recurring errors must be visible in run summaries. Preparation failures produce abstentions for affected questions, never fabricated missing-data evidence.

**18. Worked example**

Question: `sp bán dc nhiều nhất tuần rồi là gì`, as of 2026-03-04.

1. Planner identifies product ranking by base-unit quantity, previous week, top one, string output.
2. Calendar resolves 2026-02-23 inclusive to 2026-03-02 exclusive.
3. Capability check establishes product identity, quantity, date, and status availability.
4. Rule registry selects revenue-eligible orders and excludes non-product fee lines from ranking.
5. Compiler joins lines to unique order/product dimensions, converts quantities exactly once, groups by product, and sorts by quantity descending then source code ascending.
6. Verifier checks join cardinality, complete unit mapping, and ranking order.
7. Serializer retrieves the winning source name and writes it as a string. The actual winner is computed from the business data; the example's answer is never stored as a rule.

Schematic query over already validated canonical tables:

```sql
SELECT p.product_key, p.source_code, p.source_name,
       SUM(l.quantity_base) AS metric_value
FROM sales_lines l
JOIN orders o ON o.order_key = l.order_key
JOIN products p ON p.product_key = l.product_key
WHERE o.placed_at >= ? AND o.placed_at < ?
  AND o.is_revenue_eligible = TRUE
  AND p.is_rankable_product = TRUE
GROUP BY p.product_key, p.source_code, p.source_name
ORDER BY metric_value DESC, p.source_code ASC
LIMIT 1;
```

Eligibility columns are derived from the current validated rule registry, including return evidence. No-code entity sorting uses the rulebook's name fallback, not an invented surrogate-code order. If no candidate exists and the rulebook does not define the requested single-winner result, do not invent an empty string.

**19. Evaluation and training strategy**

Build an approximate local scorer from the documented rules: exact type, monetary/percentage rounding, Unicode NFC and permitted string normalization, ordered lists, and table matching. The organizer's precise floating tolerance and some edge cases are not fully specified; document those approximations. Unit-test scorer behavior independently of model behavior.

Create reproducible business-level splits, keeping a development group, a confidence-calibration group, and an untouched comparison group. Balance schema families and industries as feasible. Five-fold business validation is a later option; it must not leak held-out questions through prompt examples or generated traces. Add industry-held-out stress tests where the data permits.

Model comparisons use the same ingestion, compiler, rules, and question sets:

- Qwen3.5-9B non-thinking.
- Qwen3.5-9B thinking.
- Qwen3.5-9B selective thinking.
- Optional earlier Qwen3-14B baseline as a separate experiment, never bundled with 9B in the final inference inventory.

Track PA, attempted-answer accuracy, coverage, unanswerable precision/recall, schema failures, date/entity/metric errors, format failures, median/p95 latency, peak memory, cold-run time, and repeat-run agreement. Compute uncertainty by resampling businesses rather than treating thousands of related questions as independent evidence.

Meaningful tests include:

- An order returned after as_of_date is excluded from revenue when rules require it.
- Returned debt remains when unpaid and eligible.
- Repeated identical orderless rows remain separate events.
- Group fill does not cross order boundaries.
- Two order lines and three payments do not multiply debt/revenue.
- A snapshot at the wrong date does not prove historical stock.
- Mixed units use product-specific conversion once.
- A month-to-date comparison truncates the previous month correctly.
- A Tết interval is resolved from the supplied calendar.
- A changed split rule actually changes the compiled calculation.
- Changing column names or file order preserves a supported answer.

Synthetic data should be generated by known programs with exact expected results, including missing-data counterexamples. Keep synthetic stress-test metrics separate from held-out real-data scores. Never train on public/private test questions, answers, or generated traces.

Fine-tune only after error analysis shows a language/planning bottleneck. Candidate training targets are schema mappings, plans, targeted repairs, and missing-capability decisions. LoRA/QLoRA can be investigated on compatible hardware, but it is not required for the first working agent. Re-evaluate a merged or quantized checkpoint and account for all inference parameters.

**20. Implementation milestones and acceptance gates**

| Milestone | Deliverable | Gate before moving on |
|---|---|---|
| A | Scorer, contracts, config, notebook preflight | Hand-written valid/invalid submissions score as intended |
| B | Readers and profiles for CSV/XLSX/SQLite | Files read without unexplained dropped rows; raw provenance retained |
| C | Schema mapping and capability catalog | Representative formats have validated grain and joins |
| D | Rule registry, calendar, basic metrics | Edge-case semantic tests pass |
| E | Qwen backend and planner | Local checkpoint loads; final plans parse; failures are explicit |
| F | End-to-end sales/count/ranking/ratio baseline | Business-held-out report produced |
| G | Debt, stock, profit, industry extensions | Targeted calculations pass independent checks |
| H | Repair and calibrated decisions | PA improves on development/calibration without final-set tuning |
| I | Offline full run and resume | No downloads; interrupted run resumes with identical completed outputs |
| J | Docker rehearsal | Reference-hardware cold run meets time and reproducibility requirements |

Do not begin with fine-tuning, a second model, or many sampled plans per question. Establish error attribution first. Prioritize the modules causing the largest number of wrong answers, weighted by how broadly a fix generalizes.

**21. Kaggle-to-Docker handoff**

The final entry point should be equivalent to:

```bash
python -m datamate.cli predict \
  --split /data \
  --model /opt/models/qwen35-9b \
  --work /result \
  --config /opt/datamate/configs/docker.yaml
```

`predict.sh` invokes this command and preserves its exit status. Package source, training code, pinned weights/tokenizer, dependency lock, licenses, model inventory, and README. Do not bake test answers or identifier-specific rules into the image. Include derived artifacts only when permitted and reproducible from allowed inputs; test-business preparation must run automatically on the mounted package.

Test with read-only `/data`, writable `/result`, and network disabled. Use the organizer-specified Docker base and Blackwell-compatible stack. Pin the Docker image digest and record a checksum for the delivered package.

The same model revision, prompts, semantics, and serializer should run in Kaggle and Docker. If hardware requires a different precision/backend, compare identical inputs and quantify changes before freezing the solution. The uploaded private answers must come from the frozen solution configuration, and runtime optimization must not introduce unreviewed semantic changes.

**22. Recommended initial build**

Implement a single-model, text-only Qwen3.5-9B planner with a validated Kaggle backend, deterministic ingestion and SQL execution, a split-loaded rule registry, three-state capabilities, one bounded repair, and a resumable output journal. Start with common sales metrics and expand using held-out error reports. This creates a practical baseline that can be debugged in a notebook and transferred to the required offline container without rewriting the agent.

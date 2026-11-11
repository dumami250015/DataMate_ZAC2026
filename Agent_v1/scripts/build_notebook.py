"""Regenerate the portable Kaggle runner. Uses only the Python standard library."""
import json
from pathlib import Path

cells = []
def md(text):
    cells.append({"cell_type": "markdown", "metadata": {}, "source": text.splitlines(True)})
def code(text):
    cells.append({"cell_type": "code", "metadata": {}, "source": text.splitlines(True), "outputs": [], "execution_count": None})

md("""# DataMate v1 — Kaggle runner
Attach the **source package**, **DataMate data**, **complete Qwen3.5-9B checkpoint**, and an optional **compatible wheelhouse**. Select a GPU. Edit the four paths below. This notebook executes local code and local weights; it does not call an external model API.

Run cells in order. Start with a small training subset. The full run is enabled explicitly in Cell 10. A GPU smoke test is required; 4-bit Qwen3.5 compatibility is hardware/version dependent. Read `README.md` for environment preparation and troubleshooting.
""")
md("## 1. Attached paths")
code('''from pathlib import Path
import os, sys
SOURCE = Path("/kaggle/input/datamate-source/Agent_v1")
SPLIT = Path("/kaggle/input/datamate-data/train")
MODEL = Path("/kaggle/input/qwen35-9b-checkpoint")
WHEELS = Path("/kaggle/input/datamate-wheels")
WORK = Path("/kaggle/working/datamate")
WORK.mkdir(parents=True, exist_ok=True)
assert (SOURCE / "datamate" / "cli.py").is_file(), "Point SOURCE to the extracted Agent_v1 folder"
assert (SPLIT / "questions.jsonl").is_file()
assert (MODEL / "config.json").is_file(), "Point MODEL to the actual checkpoint root, not its parent"
refreshed_source = None
sys.path.insert(0, str(SOURCE))
os.environ["PYTHONPATH"] = str(SOURCE) + os.pathsep + os.environ.get("PYTHONPATH", "")
print({"source": str(SOURCE), "split": str(SPLIT), "model": str(MODEL), "work": str(WORK)})
''')
md("## 2. Hardware inspection (before dependency changes)")
code('''import platform, shutil, subprocess, importlib.util
print("Python", platform.python_version(), "Free disk GiB", shutil.disk_usage(WORK).free / 2**30)
subprocess.run(["nvidia-smi"], check=False)
if importlib.util.find_spec("torch"):
    import torch
    print("Torch", torch.__version__, "CUDA", torch.version.cuda)
    for i in range(torch.cuda.device_count()):
        print(i, torch.cuda.get_device_name(i), torch.cuda.get_device_capability(i), torch.cuda.get_device_properties(i).total_memory / 2**30)
''')
md("## 3. Dependencies\nUse an attached compatible wheelhouse for offline installation. For an initial setup-only session, you may enable Kaggle Internet and set OFFLINE_INSTALL=False; disable Internet again before the reproducibility run. If Torch or Transformers is replaced after import, restart the kernel and rerun with INSTALL_DEPENDENCIES=False. Do not blindly replace Kaggle's CUDA Torch build.")
code('''INSTALL_DEPENDENCIES = False
OFFLINE_INSTALL = True
if INSTALL_DEPENDENCIES:
    command = [sys.executable, "-m", "pip", "install"]
    if OFFLINE_INSTALL:
        assert WHEELS.is_dir(), "Attach a wheelhouse or use an Internet-enabled setup session"
        command += ["--no-index", "--find-links", str(WHEELS)]
    command += ["-r", str(SOURCE / "requirements-kaggle.txt")]
    subprocess.check_call(command)
required = ["duckdb", "sqlglot", "pydantic", "yaml", "openpyxl", "simplejson", "pandas", "torch", "transformers", "accelerate", "bitsandbytes"]
missing = [name for name in required if importlib.util.find_spec(name) is None]
assert not missing, f"Missing dependencies: {missing}. Attach a compatible wheelhouse or prepare the environment first."
''')
md("## 4. Offline configuration\nThe Kaggle NF4/FP16 profile processes long prompts in 512-token chunks and retains the full prompt.")
code('''for key in ["HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"]:
    os.environ[key] = "1"
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logging.getLogger("datamate").setLevel(logging.INFO)
from importlib import import_module, invalidate_caches
import shutil
import tempfile

required_source_version = "1.0.4"
expected_config = (SOURCE / "datamate/config.py").resolve()
version_file = SOURCE / "datamate/__init__.py"
if (
    f'__version__ = "{required_source_version}"' not in version_file.read_text(encoding="utf-8")
    or "prefill_chunk_size:" not in expected_config.read_text(encoding="utf-8")
):
    raise RuntimeError(
        f"The file on disk at {expected_config} is from an older DataMate source. "
        "Attach the complete updated Agent_v1_source.zip and rerun Cell 1."
    )

import_root = SOURCE
if globals().get("refreshed_source") == SOURCE:
    import_root = refreshed_import_root

datamate_config = import_module("datamate.config")
loaded_config = Path(datamate_config.__file__).resolve()
if loaded_config != (import_root / "datamate/config.py").resolve() or (
    getattr(import_module("datamate"), "__version__", None) != required_source_version
    or "prefill_chunk_size" not in datamate_config.ModelConfig.model_fields
):
    # Import a clean copy of the selected package, including its sibling modules.
    # A new directory avoids reusing cached bytecode from a mounted dataset.
    import_root = Path(tempfile.mkdtemp(prefix="datamate-import-", dir=WORK))
    shutil.copytree(SOURCE / "datamate", import_root / "datamate",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    globals().pop("agent", None)
    for name in tuple(sys.modules):
        if name == "datamate" or name.startswith("datamate."):
            del sys.modules[name]
    sys.path.insert(0, str(import_root))
    invalidate_caches()
    datamate_config = import_module("datamate.config")
    loaded_config = Path(datamate_config.__file__).resolve()
    if loaded_config != (import_root / "datamate/config.py").resolve() or (
        getattr(import_module("datamate"), "__version__", None) != required_source_version
        or "prefill_chunk_size" not in datamate_config.ModelConfig.model_fields
    ):
        raise RuntimeError(f"Could not import the updated DataMate package from {import_root}.")
    refreshed_source, refreshed_import_root = SOURCE, import_root
    print("Refreshed DataMate imports from the selected source.")

worker_paths = os.environ.get("PYTHONPATH", "").split(os.pathsep)
os.environ["PYTHONPATH"] = os.pathsep.join(
    [str(import_root), *(path for path in worker_paths if path and path != str(import_root))]
)
print("DataMate config loaded from:", loaded_config)
from datamate.runtime import environment
from datamate.util import write_json
cfg = datamate_config.Config.load(SOURCE / "configs/kaggle.yaml", split=SPLIT, model=MODEL, work=WORK)
# Example for a supported >=32GB GPU: cfg.model.quantization="none"; cfg.model.dtype="bfloat16"
# For a one-GPU NF4 run, cfg.model.device_map="cuda:0" can avoid unintended CPU offload.
cfg.runtime.fail_fast = True  # surface programming/environment problems during development
cfg.validate_paths()
write_json(WORK / "environment.json", environment(WORK))
print(cfg.model_dump())
''')
md("## 5. Load once and smoke-test\nThis fingerprints local weights, loads Qwen, and checks structured generation. Stop here if it fails. No random-weight fallback or automatic model download is permitted.")
code('''from datamate.pipeline import Agent, read_questions
agent = Agent(cfg)
print("Backend ready:", agent.backend.identity)
''')
md("## 6. Select a development business\nIDs here only select input data; the agent has no identifier-specific answer logic.")
code('''questions = read_questions(SPLIT)
DEV_BUSINESSES = [questions[0].business_id]  # Set to None for split-wide selection.
DEV_OFFSET = 0  # Set to 20 to start at question 21.
DEV_LIMIT = 20
selected = read_questions(SPLIT, DEV_BUSINESSES, DEV_LIMIT, offset=DEV_OFFSET)
print("Total", len(questions), "Development", len(selected), "Business", DEV_BUSINESSES)
''')
md("## 7. Inspect prepared schema and parsing diagnostics")
code('''context = agent.prepare(selected[0].business_id)
for table in context["catalog"]:
    print(table["table"], table["role"], table["grain"], "rows=", table["row_count"],
          "duplicate_rows=", table.get("duplicate_rows", 0), "parse_errors=", table["parse_errors"])
print("Database:", context["database"])
''')
md("## 8. End-to-end development inference")
code('''def diagnose(run):
    subprocess.run([sys.executable, "-X", "utf8", str(SOURCE / "scripts/diagnose_run.py"), str(run)], check=True)

dev_run = agent.predict(DEV_BUSINESSES, DEV_LIMIT, offset=DEV_OFFSET)
print("Development run:", dev_run)
diagnose(dev_run)
''')
md("## 9. Training-only evaluation\nReference answers are read only by this scorer, never by the predictor. selected_only=True scores the development subset; full competition scores use all questions.")
code('''from datamate.evaluation import evaluate, read_jsonl
if (SPLIT / "answers.jsonl").is_file():
    metrics = evaluate(dev_run / "submission.jsonl", SPLIT / "answers.jsonl", SPLIT / "questions.jsonl", dev_run / "metrics.json", selected_only=True)
    print(metrics)
    references = read_jsonl(SPLIT / "answers.jsonl")
    for qid, record in read_jsonl(dev_run / "submission.jsonl").items():
        expected = references[qid]
        print(qid, "predicted", record["type"], record.get("answer"),
              "expected", expected["type"], expected.get("answer"))
else:
    print("No released references for this split; inference only.")
''')
md("## 10. Full prediction / resume\nEnable after inspecting the development logs. The current Agent's deadline includes its loading and prior work. For a fresh timed run, restart and load once. To resume after a session interruption, restore saved work outputs and use identical source, input files, config, backend and model. Prior partial answers are fingerprint-checked.")
code('''RUN_FULL = False
if RUN_FULL:
    cfg.runtime.fail_fast = False
    full_run = agent.predict()
    print("Full run:", full_run)
    diagnose(full_run)
else:
    print("Set RUN_FULL=True to process all questions.")
''')
md("## 11. Validate and save outputs\nDownload/save the submission, run logs, configuration and manifest as Kaggle notebook outputs. A development-only submission intentionally lacks the other IDs and is not a full submission.")
code('''from datamate.evaluation import read_jsonl
from datamate.output import validate_record
submission = WORK / "submission.jsonl"
records = read_jsonl(submission)
for record in records.values():
    validate_record(record)
expected = {q.id for q in questions}
print("Records:", len(records), "Missing IDs:", len(expected - records.keys()), "Extra IDs:", len(records.keys() - expected))
print("Submission:", submission)
print("Run artifacts:", WORK / "runs")
if RUN_FULL:
    assert set(records) == expected
''')

notebook = {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}, "language_info": {"name": "python", "version": "3.11"}}, "nbformat": 4, "nbformat_minor": 5}
for i, cell in enumerate(cells):
    cell["id"] = f"datamate-{i:03d}"
path = Path(__file__).resolve().parents[1] / "notebooks" / "01_datamate_runner.ipynb"
path.parent.mkdir(exist_ok=True)
path.write_text(json.dumps(notebook, ensure_ascii=False, indent=2), encoding="utf-8")
print(path)


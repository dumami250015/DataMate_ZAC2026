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
md("## 4. Offline configuration\nThe default is a candidate NF4/FP16 profile. Context is checked without silent truncation. Increase/decrease only after testing memory and accuracy; do not treat two 16 GB GPUs as one contiguous 32 GB GPU.")
code('''for key in ["HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"]:
    os.environ[key] = "1"
from datamate.config import Config
from datamate.runtime import environment
from datamate.util import write_json
cfg = Config.load(SOURCE / "configs/kaggle.yaml", split=SPLIT, model=MODEL, work=WORK)
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
DEV_BUSINESSES = [questions[0].business_id]
DEV_LIMIT = 20
selected = read_questions(SPLIT, DEV_BUSINESSES, DEV_LIMIT)
print("Total", len(questions), "Development", len(selected), "Business", DEV_BUSINESSES)
''')
md("## 7. Inspect prepared schema and parsing diagnostics")
code('''context = agent.prepare(DEV_BUSINESSES[0])
for table in context["catalog"]:
    print(table["table"], table["role"], table["grain"], "rows=", table["row_count"], "parse_errors=", table["parse_errors"])
print("Database:", context["database"])
''')
md("## 8. End-to-end development inference")
code('''dev_run = agent.predict(DEV_BUSINESSES, DEV_LIMIT)
print("Development run:", dev_run)
print((dev_run / "submission.jsonl").read_text(encoding="utf-8")[:3000])
''')
md("## 9. Training-only evaluation\nReference answers are read only by this scorer, never by the predictor. selected_only=True scores the development subset; full competition scores use all questions.")
code('''from datamate.evaluation import evaluate
if (SPLIT / "answers.jsonl").is_file():
    metrics = evaluate(dev_run / "submission.jsonl", SPLIT / "answers.jsonl", SPLIT / "questions.jsonl", dev_run / "metrics.json", selected_only=True)
    print(metrics)
else:
    print("No released references for this split; inference only.")
''')
md("## 10. Full prediction / resume\nEnable after inspecting the development logs. The current Agent's deadline includes its loading and prior work. For a fresh timed run, restart and load once. To resume after a session interruption, restore saved work outputs and use identical source, input files, config, backend and model. Prior partial answers are fingerprint-checked.")
code('''RUN_FULL = False
if RUN_FULL:
    cfg.runtime.fail_fast = False
    full_run = agent.predict()
    print("Full run:", full_run)
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


"""Run AFTER successful Kaggle/GPU tests. Never label an untested environment validated."""
import argparse
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True)
args = parser.parse_args()
text = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
Path(args.output).write_text(text, encoding="utf-8")
print("Recorded environment; build the wheelhouse on the same Python/CUDA platform.")


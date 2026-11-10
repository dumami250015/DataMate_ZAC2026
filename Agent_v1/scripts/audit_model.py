"""Inspect safetensors headers without allocating tensors or importing GPU libraries."""
import argparse
import hashlib
import json
import math
import struct
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("model")
parser.add_argument("--output")
args = parser.parse_args()
root = Path(args.model)
config = json.loads((root / "config.json").read_text())
files, total, seen = [], 0, set()
for path in sorted(root.glob("*.safetensors")):
    with path.open("rb") as f:
        length = struct.unpack("<Q", f.read(8))[0]
        if length > 100 * 1024 * 1024:
            raise ValueError("Implausible safetensors header size")
        header = json.loads(f.read(length))
    elements = 0
    for name, value in header.items():
        if name == "__metadata__":
            continue
        if name in seen:
            raise ValueError(f"Duplicate tensor name across shards: {name}")
        seen.add(name)
        elements += math.prod(value["shape"])
    total += elements
    files.append({"file": path.name, "bytes": path.stat().st_size, "stored_elements": elements})
if not files:
    raise ValueError("No safetensors shards found")
report = {"architecture": config.get("architectures"), "files": files, "stored_elements": total,
          "quantization_config": config.get("quantization_config"),
          "note": "For an unquantized checkpoint this includes all stored tensors, including vision/MTP and buffers. Quantized stored elements are NOT the original parameter count. Audit original weights and all auxiliary models for competition compliance."}
text = json.dumps(report, indent=2)
print(text)
if args.output:
    Path(args.output).write_text(text, encoding="utf-8")


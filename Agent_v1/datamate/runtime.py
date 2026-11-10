import importlib.metadata
import platform
import shutil
import time
from pathlib import Path
from .util import digest, file_hash


class Deadline:
    def __init__(self, seconds, reserve=0):
        self.started = time.monotonic()
        self.end = self.started + seconds
        self.reserve = reserve

    @property
    def remaining(self):
        return max(0.0, self.end - time.monotonic() - self.reserve)

    def check(self):
        if self.remaining <= 0:
            raise TimeoutError("Run deadline reached")


def environment(work):
    out = {"python": platform.python_version(), "platform": platform.platform(), "free_disk": shutil.disk_usage(work).free, "packages": {}}
    for name in ["duckdb", "sqlglot", "pydantic", "openpyxl", "transformers", "torch", "accelerate", "bitsandbytes", "vllm"]:
        try:
            out["packages"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    try:
        import torch
        out["cuda"] = torch.version.cuda
        out["gpus"] = [{"name": torch.cuda.get_device_name(i), "capability": torch.cuda.get_device_capability(i), "memory": torch.cuda.get_device_properties(i).total_memory} for i in range(torch.cuda.device_count())]
    except ImportError:
        out["gpus"] = []
    return out


def source_fingerprint():
    root = Path(__file__).parent
    return digest({str(p.relative_to(root)): file_hash(p) for p in sorted(root.rglob("*.py"))})


def checkpoint_fingerprint(path):
    root = Path(path)
    if not root.is_dir():
        return "no-model"
    files = sorted(p for p in root.rglob("*") if p.is_file())
    # Hash every checkpoint artifact, including weights: no stale cache after local edits.
    return digest({str(p.relative_to(root)): file_hash(p) for p in files})


"""Create a Kaggle-uploadable source zip, excluding caches/dependencies/weights."""
from pathlib import Path
import zipfile
import hashlib

root = Path(__file__).resolve().parents[1]
target = root / "dist" / "Agent_v1_source.zip"
target.parent.mkdir(exist_ok=True)
excluded = {".deps", ".venv", "__pycache__", ".pytest_cache", "work", "dist", "wheelhouse", "models", ".git"}
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_file() and not (set(relative.parts) & excluded) and not any(p.endswith(".egg-info") for p in relative.parts):
            archive.write(path, "Agent_v1/" + relative.as_posix())
checksum = hashlib.sha256(target.read_bytes()).hexdigest()
target.with_suffix(target.suffix + ".sha256").write_text(checksum + "  " + target.name + "\n", encoding="utf-8")
print(target)
print("SHA256:", checksum)


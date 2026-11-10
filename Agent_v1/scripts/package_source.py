"""Create a Kaggle-uploadable source zip, excluding caches/dependencies/weights."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
target = root / "dist" / "Agent_v1_source.zip"
target.parent.mkdir(exist_ok=True)
excluded = {".deps", ".venv", "__pycache__", ".pytest_cache", "work", "dist", "wheelhouse", "models", ".git"}
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_file() and not (set(relative.parts) & excluded) and not any(p.endswith(".egg-info") for p in relative.parts):
            archive.write(path, "Agent_v1/" + relative.as_posix())
print(target)


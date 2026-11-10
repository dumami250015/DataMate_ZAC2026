import re
from pathlib import Path
from .util import file_hash, norm


class Rulebook:
    def __init__(self, path):
        self.path = Path(path)
        self.hash = file_hash(path)
        self.text = self.path.read_text(encoding="utf-8-sig")
        self.clauses = {}
        section = "preamble"
        for line in self.text.splitlines():
            if line.startswith("##"):
                section = line.strip("# ")
            if line.strip():
                key = f"R{len(self.clauses) + 1:03d}"
                self.clauses[key] = {"section": section, "text": line}

    def prompt(self):
        return "\n".join(f"[{k}] {v['text']}" for k, v in self.clauses.items())

    def validate_refs(self, refs):
        unknown = set(refs) - self.clauses.keys()
        if unknown:
            raise ValueError(f"Invented rule references: {sorted(unknown)}")


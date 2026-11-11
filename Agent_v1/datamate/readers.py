from __future__ import annotations
import csv
import random
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from .normalize import infer_decimal_separator
from .util import file_hash, digest, quote


@dataclass
class Source:
    path: Path
    sheet: str
    kind: str

    def rows(self):
        if self.kind == "csv":
            encoding = "utf-8-sig"
            try:
                with self.path.open(encoding=encoding) as f:
                    sample = f.read(16384)
            except UnicodeDecodeError:
                encoding = "cp1258"
                with self.path.open(encoding=encoding) as f:
                    sample = f.read(16384)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            with self.path.open(encoding=encoding, newline="") as f:
                yield from csv.reader(f, dialect)
        elif self.kind == "xlsx":
            import openpyxl
            book = openpyxl.load_workbook(self.path, read_only=True, data_only=True)
            try:
                yield from book[self.sheet].iter_rows(values_only=True)
            finally:
                book.close()
        else:
            connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                cursor = connection.execute(f"SELECT * FROM {quote(self.sheet)}")
                yield [c[0] for c in cursor.description]
                yield from cursor
            finally:
                connection.close()


def discover(root):
    sources = []
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or path.name.startswith("~$"):
            continue
        ext = path.suffix.lower()
        if ext == ".csv":
            sources.append(Source(path, "", "csv"))
        elif ext == ".xlsx":
            import openpyxl
            book = openpyxl.load_workbook(path, read_only=True, data_only=True)
            sources.extend(Source(path, s, "xlsx") for s in book.sheetnames)
            book.close()
        elif ext in (".sqlite", ".sqlite3", ".db"):
            con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            tables = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            con.close()
            sources.extend(Source(path, t[0], "sqlite") for t in tables)
    if not sources:
        raise ValueError(f"No supported business data files in {root}")
    return sources


def manifest(root):
    root = Path(root)
    return {str(p.relative_to(root)): file_hash(p) for p in sorted(root.rglob("*")) if p.is_file()}


def preview(source, count=30):
    result = []
    iterator = source.rows()
    try:
        for i, row in enumerate(iterator, 1):
            result.append({"row": i, "values": [None if v is None else str(v) for v in row]})
            if i >= count:
                break
    finally:
        iterator.close()
    return result


def profile_rows(source, header_row=1, samples=6):
    headers, reservoir, count, nulls, prefix = [], [], 0, [], []
    rng = random.Random(42)
    format_examples = {}
    for index, row in enumerate(source.rows(), 1):
        if index < header_row:
            continue
        if index == header_row:
            names = [str(x).strip() if x is not None and str(x).strip() else f"unnamed_{i}" for i, x in enumerate(row)]
            reserved = set(names)
            for i, name in enumerate(names):
                label = name
                if label in headers:
                    label = f"{name} [{i + 1}]"
                    while label in reserved:
                        label += "_"
                    reserved.add(label)
                headers.append(label)
            nulls = [0] * len(headers)
            continue
        if not any(x is not None and str(x).strip() for x in row):
            continue
        vals = [None if x is None else str(x) for x in row]
        vals = (vals + [None] * len(headers))[:len(headers)]
        count += 1
        for i, v in enumerate(vals):
            nulls[i] += v is None or not v.strip()
            if i < len(row) and isinstance(row[i], str) and v and ("," in v or "." in v):
                parts = re.split(r"[.,]", v.strip())
                if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) != 3):
                    separator = infer_decimal_separator([v], "auto")
                    if separator != "auto":
                        format_examples.setdefault(i, {}).setdefault(separator, v)
        item = ({"row": index, "values": vals}, list(row))
        if len(prefix) < samples:
            prefix.append(item[1])
        if len(reservoir) < samples:
            reservoir.append(item)
        else:
            j = rng.randrange(count)
            if j < samples:
                reservoir[j] = item
    reservoir.sort(key=lambda item: item[0]["row"])
    return {"headers": headers, "row_count": count, "null_counts": nulls,
            "samples": [item for item, _ in reservoir], "format_examples": format_examples,
            "text_samples": [[v if isinstance(v, str) else None for v in row]
                             for row in prefix + [row for _, row in reservoir]]}


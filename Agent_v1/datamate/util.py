from __future__ import annotations
import hashlib
import os
import re
import unicodedata
from pathlib import Path
import simplejson as json


def dumps(value, **kwargs):
    return json.dumps(value, ensure_ascii=False, use_decimal=True, allow_nan=False, **kwargs)


def loads(text):
    return json.loads(text, use_decimal=True)


def read_json(path):
    return loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def write_json(path, value):
    atomic_text(path, dumps(value, indent=2))


def digest(value):
    return hashlib.sha256(dumps(value, sort_keys=True).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"Unsafe identifier: {value!r}")
    return value


def norm(value, accents=False):
    value = unicodedata.normalize("NFC", str(value)).lower()
    if accents:
        value = "".join(c for c in unicodedata.normalize("NFD", value) if not unicodedata.combining(c)).replace("đ", "d")
    return " ".join("".join(c if c.isalnum() or c.isspace() else " " for c in value).split())


def quote(name):
    return '"' + name.replace('"', '""') + '"'


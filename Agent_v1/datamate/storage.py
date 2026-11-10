from __future__ import annotations
import re
from pathlib import Path
import duckdb
import pandas as pd
from .contracts import TableMapping
from .readers import discover, manifest, preview, profile_rows
from .normalize import number, parse_date, year_from_sheet
from .prompts import MAPPING
from .capabilities import catalog_capabilities
from .util import digest, dumps, quote, read_json, write_json


def valid_mapping(mapping, headers):
    source_names = [c.source for c in mapping.columns]
    semantic_names = [c.semantic for c in mapping.columns]
    if len(set(headers)) != len(headers):
        raise ValueError("Duplicate source headers require explicit source preprocessing")
    if len(set(source_names)) != len(source_names) or len(set(semantic_names)) != len(semantic_names):
        raise ValueError("Mapping columns must be unique")
    for c in mapping.columns:
        if c.source not in headers or not re.fullmatch(r"[a-z][a-z0-9_]*", c.semantic):
            raise ValueError(f"Invalid mapping {c}")
        if c.semantic.startswith("c_"):
            raise ValueError("c_ prefix is reserved for raw columns")
    if mapping.group_key and mapping.group_key not in headers:
        raise ValueError("Unknown group key")
    if mapping.fill_columns and not mapping.group_key:
        raise ValueError("Grouped fill requires a verified key")
    if set(mapping.fill_columns) - set(headers):
        raise ValueError("Unknown fill column")
    if mapping.deduplicate_within_order and not mapping.group_key:
        raise ValueError("Deduplication requires order identity")


def raw_mapping(headers):
    return TableMapping(columns=[{"source": h, "semantic": f"field_{i:03d}", "kind": "text"} for i, h in enumerate(headers)])


def bulk_insert(connection, table, rows):
    """Vectorized ingestion; Decimal objects remain exact until the explicit target cast."""
    frame = pd.DataFrame(rows, columns=[f"v{i}" for i in range(len(rows[0]))], dtype=object)
    connection.register("_ingest_batch", frame)
    try:
        connection.execute(f"INSERT INTO {quote(table)} SELECT * FROM _ingest_batch")
    finally:
        connection.unregister("_ingest_batch")


def build_business(root, cache_root, rules, backend=None, samples=6, deadline=None):
    """All raw cells retained. Normalized families are unioned only by schema and file stem."""
    root, cache_root = Path(root), Path(cache_root)
    files = manifest(root)
    key = digest({"files": files, "rules": rules.hash, "mapper": backend.identity if backend else "raw-only", "samples": samples})
    target = cache_root / root.name / key
    done = target / "context.json"
    if done.exists() and (target / "business.duckdb").exists():
        context = read_json(done)
        context["database"] = str((target / "business.duckdb").resolve())
        return context
    target.mkdir(parents=True, exist_ok=True)
    db_path = target / "business.duckdb"
    if db_path.exists():
        db_path.unlink()  # incomplete build only, confined to fingerprinted cache
    readme_path = root / "README.md"
    readme = readme_path.read_text(encoding="utf-8-sig") if readme_path.exists() else ""
    con = duckdb.connect(str(db_path))
    con.execute("BEGIN TRANSACTION")
    catalog, groups, mapping_cache = [], {}, {}
    try:
        for source in discover(root):
            if deadline:
                deadline.check()
            first = preview(source)
            preliminary = profile_rows(source, samples=samples)
            family = re.sub(r"[_ -]\d+$", "", source.path.stem)
            mapping_key = digest({"family": family, "headers": preliminary["headers"]})
            if mapping_key in mapping_cache:
                mapping = mapping_cache[mapping_key]
            elif backend:
                payload = {"file": source.path.name, "sheet": source.sheet, "README": readme, "preview": first[:12], "profile": preliminary,
                           "file_rules": {k: v for k, v in rules.clauses.items() if "Cách ghi" in v["section"]}}
                mapping = backend.structured(MAPPING, payload, TableMapping, thinking=False, deadline=deadline)
                mapping_cache[mapping_key] = mapping
            else:
                mapping = raw_mapping(preliminary["headers"])
            profile = preliminary if mapping.header_row == 1 else profile_rows(source, mapping.header_row, samples)
            headers = profile["headers"]
            valid_mapping(mapping, headers)
            signature = digest({"family": family, "headers": headers, "mapping": mapping.model_dump()})
            if signature not in groups:
                name = f"t{len(groups):03d}"
                groups[signature] = {"name": name, "seen": set()}
                fields = [(f"c_{i:03d}", "VARCHAR") for i in range(len(headers))]
                types = {"text": "VARCHAR", "number": "DECIMAL(38,8)", "date": "DATE", "datetime": "TIMESTAMP"}
                fields += [(c.semantic, types[c.kind]) for c in mapping.columns]
                fields += [("_file", "VARCHAR"), ("_sheet", "VARCHAR"), ("_row", "BIGINT"), ("_parse_errors", "VARCHAR"), ("_duplicate", "BOOLEAN")]
                con.execute(f"CREATE TABLE {quote(name + '_all')} ({','.join(quote(k) + ' ' + v for k, v in fields)})")
                con.execute(f"CREATE VIEW {quote(name)} AS SELECT * FROM {quote(name + '_all')} WHERE NOT _duplicate")
                catalog.append({"table": name, "role": mapping.role, "grain": mapping.grain, "mapping": mapping.model_dump(),
                                "columns": [{"name": c.semantic, "kind": c.kind, "source": c.source} for c in mapping.columns],
                                "raw_columns": {f"c_{i:03d}": h for i, h in enumerate(headers)}, "sources": [], "row_count": 0,
                                "parse_errors": {}, "null_counts": {}, "samples": []})
            group = groups[signature]
            item = next(x for x in catalog if x["table"] == group["name"])
            item["sources"].append({"file": source.path.name, "sheet": source.sheet})
            indices = {h: i for i, h in enumerate(headers)}
            buffer, active_key, filled = [], None, {}
            for row_index, row in enumerate(source.rows(), 1):
                if row_index <= mapping.header_row:
                    continue
                if not any(v is not None and str(v).strip() for v in row):
                    active_key, filled = None, {}
                    continue
                raw = [None if x is None else str(x) for x in row]
                raw = (raw + [None] * len(headers))[:len(headers)]
                if raw == headers:  # repeated printed header, explicitly identifiable
                    active_key, filled = None, {}
                    continue
                values = list(raw)
                if mapping.group_key:
                    pos = indices[mapping.group_key]
                    key_value = values[pos]
                    if key_value:
                        if key_value != active_key:
                            active_key, filled = key_value, {}
                    elif mapping.group_key in mapping.fill_columns and active_key:
                        values[pos] = active_key
                    else:
                        active_key, filled = None, {}
                    if active_key:
                        for field in mapping.fill_columns:
                            i = indices[field]
                            if values[i] not in (None, ""):
                                filled[field] = values[i]
                            elif field in filled:
                                values[i] = filled[field]
                normalized, errors = [], []
                for col in mapping.columns:
                    value = values[indices[col.source]]
                    if value is not None and not str(value).strip():
                        value = None
                    try:
                        if col.kind == "number":
                            value = number(value, col.decimal_separator)
                        elif col.kind in ("date", "datetime"):
                            value = parse_date(value, year_from_sheet(source.sheet), col.kind == "datetime")
                    except (ValueError, ArithmeticError) as e:
                        errors.append(col.semantic)
                        item["parse_errors"][col.semantic] = item["parse_errors"].get(col.semantic, 0) + 1
                        value = None
                    if value is None or value == "":
                        item["null_counts"][col.semantic] = item["null_counts"].get(col.semantic, 0) + 1
                    normalized.append(value)
                duplicate = False
                if mapping.deduplicate_within_order and active_key:
                    # Entire source-cell equality within an order, not equality after normalization.
                    identity = (active_key, tuple(raw))
                    duplicate = identity in group["seen"]
                    group["seen"].add(identity)
                buffer.append(raw + normalized + [source.path.name, source.sheet, row_index, ",".join(errors), duplicate])
                item["row_count"] += not duplicate
                if len(item["samples"]) < samples:
                    item["samples"].append(dict(zip([c.semantic for c in mapping.columns], [str(v) if v is not None else None for v in normalized])))
                if len(buffer) >= 2000:
                    bulk_insert(con, group["name"] + "_all", buffer)
                    buffer.clear()
                    if deadline:
                        deadline.check()
            if buffer:
                bulk_insert(con, group["name"] + "_all", buffer)
        entities = []
        for item in catalog:
            for col in item["columns"]:
                n = col["name"]
                if col["kind"] == "text" and any(x in n for x in ("name", "category", "status", "unit", "method", "group", "ingredient")):
                    rows = con.execute(f"SELECT DISTINCT {quote(n)} FROM {quote(item['table'])} WHERE {quote(n)} IS NOT NULL ORDER BY 1 LIMIT 10000").fetchall()
                    entities.extend({"table": item["table"], "column": n, "value": r[0]} for r in rows)
        context = {"business_id": root.name, "fingerprint": key, "database": str(db_path.resolve()), "readme": readme,
                   "catalog": catalog, "entities": entities, "source_manifest": files, "mapping_mode": "model" if backend else "raw-only"}
        context["capabilities"] = catalog_capabilities(catalog)
        write_json(target / "capabilities.json", context["capabilities"])
        write_json(target / "catalog.json", catalog)
        write_json(target / "entities.json", entities)
        write_json(target / "source_manifest.json", files)
        con.execute("COMMIT")
        con.close()
        write_json(done, context)
        return context
    finally:
        con.close()


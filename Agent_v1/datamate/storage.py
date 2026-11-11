from __future__ import annotations
import logging
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
import duckdb
import pandas as pd
from .contracts import TableMapping
from .readers import discover, manifest, preview, profile_rows
from .llm import GenerationError
from .normalize import infer_decimal_separator, money, number, parse_date, year_from_sheet
from .prompts import MAPPING
from .capabilities import catalog_capabilities
from .util import digest, norm, quote, read_json, write_json
from .verified import documented_columns, snapshot_day


logger = logging.getLogger("datamate")

MONEY_COLUMNS = {
    "line_amount", "purchase_amount", "purchase_unit_price", "unit_price", "unit_cost_selling",
    "total_payable", "payment_amount", "expense_amount",
}


def printed_subtotal(values, indices, columns):
    if any(values[indices[columns[key].source]] not in (None, "") and
           str(values[indices[columns[key].source]]).strip()
           for key in ("order_id", "product_id", "product_name", "created_at", "order_date") if key in columns):
        return False
    customer = columns.get("customer_name")
    if customer is None:
        return False
    label = norm(values[indices[customer.source]], accents=True)
    return bool(re.match(r"tong (?:ngay|thang|cong|ky)\b", label))


def valid_column(column, headers):
    return (column.source in headers and re.fullmatch(r"[a-z][a-z0-9_]*", column.semantic)
            and not column.semantic.startswith("c_"))


def valid_mapping(mapping, headers):
    source_names = [c.source for c in mapping.columns]
    semantic_names = [c.semantic for c in mapping.columns]
    if len(set(headers)) != len(headers):
        raise ValueError("Duplicate source headers require explicit source preprocessing")
    if len(set(source_names)) != len(source_names) or len(set(semantic_names)) != len(semantic_names):
        raise ValueError("Mapping columns must be unique")
    for c in mapping.columns:
        if not valid_column(c, headers):
            raise ValueError(f"Invalid mapping {c}")
    if mapping.group_key and mapping.group_key not in headers:
        raise ValueError("Unknown group key")
    if mapping.fill_columns and not mapping.group_key:
        raise ValueError("Grouped fill requires a verified key")
    if set(mapping.fill_columns) - set(headers):
        raise ValueError("Unknown fill column")
    if mapping.deduplicate_within_order and not any(
        c.source == mapping.group_key and c.semantic == "order_id" and c.kind == "text"
        for c in mapping.columns
    ):
        raise ValueError("Deduplication requires order identity")


def raw_mapping(headers):
    return TableMapping(columns=[{"source": h, "semantic": f"field_{i:03d}", "kind": "text"} for i, h in enumerate(headers)])


def map_source(backend, payload, source, samples=6, deadline=None):
    """Retry invalid proposals, then retain raw data and unambiguous mappings."""
    request = dict(payload)
    fallback = TableMapping()
    for attempt in range(2):
        mapping = None
        try:
            mapping = backend.structured(MAPPING, request, TableMapping, thinking=False, deadline=deadline)
            unique = []
            for column in mapping.columns:
                if column not in unique:
                    unique.append(column)
            mapping = mapping.model_copy(update={"columns": unique})
            profile = payload["profile"] if mapping.header_row == 1 else profile_rows(source, mapping.header_row, samples)
            headers = profile["headers"]
            if not headers and mapping.header_row != 1:
                raise ValueError("Proposed header row has no columns")
            sources = Counter(c.source for c in unique)
            semantics = Counter(c.semantic for c in unique)
            columns = [c for c in unique if valid_column(c, headers)
                       and sources[c.source] == semantics[c.semantic] == 1]
            fallback = mapping.model_copy(update={
                "columns": columns, "group_key": None, "fill_columns": [],
                "deduplicate_within_order": False,
            })
            valid_mapping(mapping, headers)
            return mapping
        except (ValueError, GenerationError) as exc:
            logger.warning("Mapping %s/%s attempt %d: %s", source.path.name, source.sheet, attempt + 1, exc)
            request = dict(payload, repair={"error": str(exc),
                                           "previous_mapping": mapping.model_dump() if mapping else None})
    logger.warning("Using partial mapping for %s/%s; original columns are available", source.path.name, source.sheet)
    return fallback.model_copy(update={"notes": fallback.notes + [
        "Mapping repair failed. Inspect the original columns and samples for unmapped fields. "
        "Grouped fill and deduplication were disabled; check the source grain before aggregating."
    ]})


def bulk_insert(connection, table, rows):
    """Vectorized ingestion without Pandas/DuckDB narrowing Decimal precision.

    DuckDB's Pandas scanner infers a DECIMAL width from values observed early in an
    object column. A later wider value in the same batch can then fail before it is
    cast to the destination DECIMAL(38,8). Transport Decimal values as canonical
    strings so the destination table owns the numeric type and precision.
    """
    transported = [
        [format(value, "f") if isinstance(value, Decimal) else value for value in row]
        for row in rows
    ]
    frame = pd.DataFrame(transported, columns=[f"v{i}" for i in range(len(rows[0]))], dtype=object)
    connection.register("_ingest_batch", frame)
    try:
        connection.execute(f"INSERT INTO {quote(table)} SELECT * FROM _ingest_batch")
    finally:
        connection.unregister("_ingest_batch")


def build_business(root, cache_root, rules, backend=None, samples=6, deadline=None, *, use_documentation=False):
    """All raw cells retained. Normalized families are unioned only by schema and file stem."""
    root, cache_root = Path(root), Path(cache_root)
    files = manifest(root)
    key = digest({"files": files, "rules": rules.hash,
                  "mapper": backend.identity if backend else "verified-only" if use_documentation else "raw-only",
                  "samples": samples})
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
    dedup_order_rows = any(
        "hai dong cua cung mot don giong nhau o moi o" in norm(clause["text"], accents=True)
        and "tinh mot lan" in norm(clause["text"], accents=True)
        for clause in rules.clauses.values()
    )
    try:
        for source in discover(root):
            if deadline:
                deadline.check()
            documented_role, columns, header_row = None, [], 1
            sample_rows = preview(source, 15) if backend or use_documentation else []
            for row in sample_rows:
                documented_role, columns = documented_columns(
                    [str(value).strip() for value in row["values"] if value is not None],
                    readme, source.path.name,
                )
                if columns:
                    header_row = row["row"]
                    break
            if use_documentation and not backend and not columns:
                continue
            preliminary = profile_rows(source, header_row=header_row if not backend else 1, samples=samples)
            family = re.sub(r"[_ -]\d+$", "", source.path.stem)
            if columns:
                family = re.sub(r"[_ -]T(?:hang)?\d{1,2}$", "", family, flags=re.I)
            mapping_key = digest({"family": family,
                                  "headers": sample_rows[header_row - 1]["values"] if columns else preliminary["headers"],
                                  "header_row": header_row,
                                  "sqlite_table": source.sheet if source.kind == "sqlite" else None})
            if mapping_key in mapping_cache:
                mapping = mapping_cache[mapping_key]
            elif backend:
                logger.info("Mapping %s/%s", source.path.name, source.sheet)
                payload = {"file": source.path.name, "sheet": source.sheet, "README": readme, "preview": sample_rows[:12],
                           "profile": {k: v for k, v in preliminary.items() if k not in ("text_samples", "format_examples")},
                           "file_rules": {k: v for k, v in rules.clauses.items() if "Cách ghi" in v["section"]}}
                mapping = map_source(backend, payload, source, samples, deadline)
                if mapping.header_row == header_row:
                    mapping_cache[mapping_key] = mapping
            elif not use_documentation:
                mapping = raw_mapping(preliminary["headers"])
            else:
                mapping = TableMapping()
            if columns:
                sources = {c.source for c in columns}
                semantics = {c.semantic for c in columns}
                extra = [c for c in mapping.columns if c.source not in sources and c.semantic not in semantics] if mapping.header_row == header_row else []
                dedup = documented_role == "sales_flat" and dedup_order_rows
                key_source = next((c.source for c in columns if c.semantic == "order_id"), None) if dedup else None
                updates = {"header_row": header_row, "columns": columns + extra,
                           "group_key": key_source, "fill_columns": [], "deduplicate_within_order": bool(key_source)}
                if documented_role == "snapshot" and (day := snapshot_day(readme)):
                    updates["coverage_end"] = day
                mapping = mapping.model_copy(update=updates)
            profile = preliminary if mapping.header_row == (header_row if not backend else 1) else profile_rows(source, mapping.header_row, samples)
            headers = profile["headers"]
            valid_mapping(mapping, headers)
            signature = digest({"family": family, "headers": headers, "mapping": mapping.model_dump(), "documented_role": documented_role,
                                "sqlite_table": source.sheet if source.kind == "sqlite" else None})
            if signature not in groups:
                name = f"t{len(groups):03d}"
                groups[signature] = {"name": name, "seen": set()}
                fields = [(f"c_{i:03d}", "VARCHAR") for i in range(len(headers))]
                types = {"text": "VARCHAR", "number": "DECIMAL(38,8)", "date": "DATE", "datetime": "TIMESTAMP"}
                fields += [(c.semantic, types[c.kind]) for c in mapping.columns]
                fields += [("_file", "VARCHAR"), ("_sheet", "VARCHAR"), ("_row", "BIGINT"), ("_parse_errors", "VARCHAR"),
                           ("_duplicate", "BOOLEAN"), ("_subtotal", "BOOLEAN")]
                con.execute(f"CREATE TABLE {quote(name + '_all')} ({','.join(quote(k) + ' ' + v for k, v in fields)})")
                con.execute(f"CREATE VIEW {quote(name)} AS SELECT * FROM {quote(name + '_all')} WHERE NOT _duplicate AND NOT _subtotal")
                catalog.append({"table": name, "role": mapping.role, "grain": mapping.grain, "mapping": mapping.model_dump(),
                                "documented_role": documented_role, "deduplicates_order_rows": mapping.deduplicate_within_order,
                                "columns": [{"name": c.semantic, "kind": c.kind, "source": c.source} for c in mapping.columns],
                                "raw_columns": {f"c_{i:03d}": h for i, h in enumerate(headers)}, "sources": [], "row_count": 0,
                                "duplicate_rows": 0, "subtotal_rows": 0, "parse_errors": {}, "null_counts": {}, "samples": []})
            group = groups[signature]
            item = next(x for x in catalog if x["table"] == group["name"])
            item["sources"].append({"file": source.path.name, "sheet": source.sheet})
            indices = {h: i for i, h in enumerate(headers)}
            mapped_sources = {col.source for col in mapping.columns}
            by_semantic = {col.semantic: col for col in mapping.columns}
            order_column = by_semantic.get("order_id") or by_semantic.get("receipt_id")
            line_column = next((by_semantic[name] for name in ("line_id", "line_number", "order_line_id", "order_line_number")
                                if name in by_semantic), None)
            line_key_positions = (
                (indices[order_column.source], indices[line_column.source])
                if order_column and order_column.kind == "text" and line_column else ()
            )
            separators = {}
            for col in mapping.columns:
                if col.kind == "number":
                    index = indices[col.source]
                    examples = [row[index] for row in profile["text_samples"] if index < len(row)]
                    examples.extend(profile["format_examples"].get(index, {}).values())
                    separators[col.semantic] = infer_decimal_separator(examples, col.decimal_separator)
            buffer, active_key, filled = [], None, {}
            original_header = None
            for row_index, row in enumerate(source.rows(), 1):
                if row_index == mapping.header_row:
                    original_header = list(row)
                if row_index <= mapping.header_row:
                    continue
                if not any(v is not None and str(v).strip() for v in row):
                    active_key, filled = None, {}
                    continue
                raw = [None if x is None else str(x) for x in row]
                raw = (raw + [None] * len(headers))[:len(headers)]
                if list(row) == original_header:  # repeated printed header, explicitly identifiable
                    active_key, filled = None, {}
                    continue
                values = (list(row) + [None] * len(headers))[:len(headers)]
                subtotal = documented_role == "sales_flat" and printed_subtotal(values, indices, by_semantic)
                if subtotal:
                    active_key, filled = None, {}
                if mapping.group_key:
                    pos = indices[mapping.group_key]
                    key_value = values[pos]
                    if key_value is not None and str(key_value).strip():
                        if key_value != active_key:
                            active_key, filled = key_value, {}
                    elif mapping.group_key in mapping.fill_columns and active_key is not None:
                        values[pos] = active_key
                    else:
                        active_key, filled = None, {}
                    if active_key is not None:
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
                            parser = money if col.semantic in MONEY_COLUMNS else number
                            value = parser(value, separators[col.semantic])
                        elif col.kind in ("date", "datetime"):
                            value = parse_date(value, year_from_sheet(source.sheet), col.kind == "datetime")
                        elif value is not None:
                            value = str(value)
                    except (ValueError, ArithmeticError):
                        errors.append(col.semantic)
                        value = None
                    normalized.append(value)
                duplicate = False
                duplicate_key = active_key if mapping.deduplicate_within_order else None
                if duplicate_key is None and line_key_positions:
                    key = tuple(raw[i] for i in line_key_positions)
                    if all(value is not None and value.strip() for value in key):
                        duplicate_key = key[0]
                if duplicate_key is not None:
                    # Entire source-cell equality within an order, not equality after normalization.
                    identity = (str(duplicate_key), tuple(raw))
                    duplicate = identity in group["seen"]
                    group["seen"].add(identity)
                buffer.append(raw + normalized + [source.path.name, source.sheet, row_index, ",".join(errors), duplicate, subtotal])
                if subtotal:
                    item["subtotal_rows"] += 1
                elif duplicate:
                    item["duplicate_rows"] += 1
                else:
                    item["row_count"] += 1
                    for name in errors:
                        item["parse_errors"][name] = item["parse_errors"].get(name, 0) + 1
                    for col, value in zip(mapping.columns, normalized):
                        if value is None or value == "":
                            item["null_counts"][col.semantic] = item["null_counts"].get(col.semantic, 0) + 1
                    if len(item["samples"]) < samples:
                        sample = {f"c_{i:03d}": raw[i] for h, i in indices.items() if h not in mapped_sources}
                        sample.update((c.semantic, str(v) if v is not None else None) for c, v in zip(mapping.columns, normalized))
                        item["samples"].append(sample)
                if len(buffer) >= 2000:
                    bulk_insert(con, group["name"] + "_all", buffer)
                    buffer.clear()
                    if deadline:
                        deadline.check()
            if buffer:
                bulk_insert(con, group["name"] + "_all", buffer)
        entities = []
        for item in catalog:
            item["distinct_values"] = {}
            date_columns = [c["name"] for c in item["columns"] if c["kind"] in ("date", "datetime")]
            item["observed_dates"] = {}
            if date_columns:
                expressions = [f"{fn}(CAST({quote(name)} AS DATE))" for name in date_columns for fn in ("MIN", "MAX")]
                bounds = con.execute(f"SELECT {', '.join(expressions)} FROM {quote(item['table'])}").fetchone()
                item["observed_dates"] = {
                    name: [bounds[i * 2].isoformat(), bounds[i * 2 + 1].isoformat()]
                    for i, name in enumerate(date_columns) if bounds[i * 2] is not None
                }
            for col in item["columns"]:
                n = col["name"]
                if col["kind"] == "text" and any(x in n for x in ("name", "category", "status", "unit", "method", "group", "ingredient")):
                    rows = con.execute(f"SELECT DISTINCT {quote(n)} FROM {quote(item['table'])} WHERE {quote(n)} IS NOT NULL ORDER BY 1 LIMIT 10000").fetchall()
                    entities.extend({"table": item["table"], "column": n, "value": r[0]} for r in rows)
                    if len(rows) <= 50:
                        item["distinct_values"][n] = [r[0] for r in rows]
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


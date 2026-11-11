"""Compile common totals from documented schemas and the active split rulebook."""
import re
from datetime import date, timedelta

from .calendar import resolve
from .contracts import ColumnMapping, Plan, TimeSpec
from .normalize import parse_date
from .util import norm, quote
from .verified_intent import parse_intent


SCHEMAS = {
    "purchase_receipts": (
        ("ma nhap hang", "receipt_id", "text", "ma phieu nhap"),
        ("thoi gian", "purchase_date", "date", "ngay nhap"),
        ("can tra ncc", "purchase_amount", "number", "tong tien phieu nhap"),
        ("trang thai", "status", "text", "trang thai phieu nhap"),
    ),
    "sales_orders": (
        ("ma hoa don", "order_id", "text", "ma don"),
        ("thoi gian tao", "created_at", "datetime", "thoi diem dat don"),
        ("trang thai", "status", "text", "trang thai don"),
    ),
    "sales_lines": (
        ("ma hoa don", "order_id", "text", "ma don"),
        ("thanh tien", "line_amount", "number", "thanh tien sau giam"),
        ("stt", "line_number", "text", "so thu tu dong trong don"),
    ),
    "customers": (
        ("ma khach hang", "customer_id", "text", "ma khach"),
        ("ten khach hang", "customer_name", "text", "ten"),
        ("nhom khach hang", "customer_group", "text", "nhom khach"),
    ),
    "products": (
        ("ma hang", "product_id", "text", "ma hang"),
        ("ten hang", "product_name", "text", "ten"),
        ("dvt", "base_unit", "text", "don vi co ban"),
    ),
    "snapshot": (
        ("ma hang", "product_id", "text", "ma hang"),
        ("ten hang", "product_name", "text", "ten hang"),
        ("dvt", "base_unit", "text", "don vi co ban"),
        ("ton cuoi ky", "snapshot_quantity", "number", "ton kho"),
    ),
    "purchase_lines": (
        ("ma nhap hang", "receipt_id", "text", "ma phieu nhap"),
        ("stt", "line_number", "text", "so thu tu dong trong phieu nhap"),
        ("ma hang", "product_id", "text", "ma hang"),
        ("dvt", "purchase_unit", "text", "don vi tinh"),
        ("so luong", "purchase_quantity", "number", "so luong"),
        ("don gia", "purchase_unit_price", "number", "gia nhap mot don vi"),
    ),
}

OPTIONAL_COLUMNS = {
    "sales_orders": (("ma kh", "customer_id", "text", "ma khach trong neu khach le"),),
    "sales_lines": (
        ("ma hang", "product_id", "text", "ma hang"),
        ("ten hang", "product_name", "text", "ten hang"),
        ("so luong quy doi", "quantity_base", "number", "da doi ra don vi co ban"),
        ("giam gia", "line_discount", "text", "giam gia"),
    ),
}

DOCUMENTED_FIELDS = {
    "order_id": ("text", r"ma don(?: hang)?\b"),
    "created_at": ("datetime", r"(?:thoi diem dat don|gio tao don|thoi gian tao don)\b"),
    "order_date": ("date", r"ngay (?:ban|dat(?: hang| don)?)\b"),
    "status": ("text", r"trang thai don\b"),
    "line_number": ("text", r"so thu tu dong trong don\b"),
    "line_amount": ("number", r"thanh tien (?:sau giam|cua dong|dong)\b"),
    "product_id": ("text", r"ma hang(?: hoa)?\b"),
    "product_name": ("text", r"ten (?:hang(?: hoa)?|san pham)\b"),
    "product_category": ("text", r"nhom hang(?: hoa)?\b"),
    "base_unit": ("text", r"don vi co ban\b"),
    "quantity_base": ("number", r"so luong (?:da )?(?:doi ra|quy doi ve) don vi co ban\b"),
    "quantity_selling": ("number", r"so luong(?: ban)?$"),
    "unit_price": ("number", r"don gia$"),
    "customer_id": ("text", r"ma khach(?: hang)?\b"),
    "customer_name": ("text", r"ten khach(?: hang)?\b"),
    "customer_group": ("text", r"nhom khach(?: hang)?\b"),
}


def document_matches_file(heading, filename):
    names = re.findall(r"`([^`]+\.(?:xlsx|csv|sqlite|db))`", heading, flags=re.I)
    if not filename or not names:
        return True
    family = re.sub(r"[_ -]\d+(?=\.)", "", filename.casefold())
    for name in names:
        pattern = re.sub(r"<[^>]+>", r"[0-9]+", re.escape(name))
        if re.fullmatch(pattern, filename, re.I) or re.sub(r"[_ -]\d+(?=\.)", "", name.casefold()) == family:
            return True
    return False


def documented_fields(headers, descriptions):
    fields = {}
    for semantic, (kind, pattern) in DOCUMENTED_FIELDS.items():
        matches = [header for header, meaning in descriptions.items() if header in headers and re.match(pattern, meaning)]
        if len(matches) == 1:
            fields[semantic] = ColumnMapping(source=headers[matches[0]], semantic=semantic, kind=kind)
    return fields


def documented_columns(headers, readme, filename=None):
    """Use columns from a single matching documentation section."""
    names = {norm(header, accents=True): header for header in headers}
    sections = [
        {norm(name, accents=True): norm(description, accents=True)
         for name, description in re.findall(r"(?m)^\s*-\s+\*\*(.+?)\*\*:\s*([^\n]+)", section)}
        for section in re.split(r"(?m)^#{2,}\s+", readme)
        if document_matches_file(section.split("\n", 1)[0], filename)
    ]
    for role in sorted(SCHEMAS, key=lambda name: -len(SCHEMAS[name])):
        fields = SCHEMAS[role]
        if not all(header in names for header, _, _, _ in fields):
            continue
        for descriptions in sections:
            if all(meaning in descriptions.get(header, "") for header, _, _, meaning in fields):
                optional = [field for field in OPTIONAL_COLUMNS.get(role, ())
                            if field[0] in names and field[3] in descriptions.get(field[0], "")]
                columns = [ColumnMapping(source=names[h], semantic=s, kind=k)
                           for h, s, k, _ in [*fields, *optional]]
                extra = documented_fields(names, descriptions)
                used_sources = {c.source for c in columns}
                used_semantics = {c.semantic for c in columns}
                columns.extend(c for c in extra.values() if c.source not in used_sources and c.semantic not in used_semantics)
                return role, columns
    candidates = []
    for descriptions in sections:
        fields = documented_fields(names, descriptions)
        keys = fields.keys()
        if {"order_id", "status"} <= keys and {"created_at", "order_date"} & keys:
            role = "sales_flat" if "line_amount" in keys else "sales_orders"
        elif {"order_id", "line_amount", "line_number"} <= keys:
            role = "sales_lines"
        elif {"product_id", "base_unit"} <= keys:
            role = "products"
        elif {"customer_id", "customer_group"} <= keys:
            role = "customers"
        else:
            continue
        if role in ("products", "customers"):
            semantic = "product_name" if role == "products" else "customer_name"
            if semantic not in fields:
                matching = [h for h, desc in descriptions.items() if h in names and desc == "ten"]
                if len(matching) == 1:
                    fields[semantic] = ColumnMapping(source=names[matching[0]], semantic=semantic)
                else:
                    continue
        if len({column.source for column in fields.values()}) == len(fields):
            candidates.append((role, list(fields.values())))
    if len(candidates) == 1:
        return candidates[0]
    return None, []


CANCELLED = {"huy", "huy don", "da huy", "da huy don", "huy phieu", "da huy phieu", "cancelled", "canceled"}
RETURNED = {"tra hang", "hang tra ve", "doi tra", "da tra hang", "tra hang mot phan", "da hoan hang", "returned"}
SALE_OK = {"xong", "thanh cong", "hoan thanh", "da hoan thanh", "dang giao hang", "dang giao", "completed", "shipping"}
PURCHASE_OK = {"da nhap hang", "da nhap", "hoan thanh", "da hoan thanh", "thanh cong", "xong"}


def documented_end(readme):
    match = re.search(r"dữ liệu có đến hết\s+(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d\d-\d\d)", readme, re.I)
    try:
        return parse_date(match.group(1)) if match else None
    except ValueError:
        return None


def snapshot_day(readme):
    if "ton cuoi ky la ton cuoi ngay cuoi co du lieu" in norm(readme, accents=True):
        return documented_end(readme)
    return None


def covered(table, date_column, time, as_of, readme):
    start, end = resolve(time, as_of)
    observed = table.get("observed_dates", {}).get(date_column)
    if not observed or start < observed[0]:
        return False
    last = max(observed[1], documented_end(readme) or observed[1])
    return end <= (date.fromisoformat(last) + timedelta(days=1)).isoformat()


def literals(values):
    return ", ".join("'" + value.replace("'", "''") + "'" for value in sorted(set(values)))


def grouped_currency_sql(column):
    text = f"TRIM({column})"
    return (
        f"CASE WHEN REGEXP_MATCHES({text}, '^[+-]?[0-9]{{1,3}}(,[0-9]{{3}})+$') "
        f"THEN TRY_CAST(REPLACE({text}, ',', '') AS DECIMAL(38,8)) "
        f"WHEN REGEXP_MATCHES({text}, '^[+-]?[0-9]{{1,3}}([.][0-9]{{3}})+$') "
        f"THEN TRY_CAST(REPLACE({text}, '.', '') AS DECIMAL(38,8)) "
        f"ELSE TRY_CAST({text} AS DECIMAL(38,8)) END"
    )


class InsufficientEvidence(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


class VerifiedQuery:
    def __init__(self, question, intent, context, rules):
        self.question, self.intent, self.context, self.rules = question, intent, context, rules
        self.refs, self.checks, self.columns = [], [], {}

    def rule(self, *phrases):
        matches = [(ref, clause["text"]) for ref, clause in self.rules.clauses.items()
                   if all(phrase in norm(clause["text"], accents=True) for phrase in phrases)]
        if len(matches) != 1:
            raise InsufficientEvidence("rule_unverified", f"Required rule not uniquely identified: {phrases[0]}")
        ref, text = matches[0]
        self.refs.append(ref)
        return text

    def table(self, role, *columns, nullable=()):
        matches = [table for table in self.context["catalog"] if table.get("documented_role") == role]
        if len(matches) != 1:
            raise InsufficientEvidence("schema_unverified", f"Expected one documented {role} table, found {len(matches)}")
        table = matches[0]
        available = {col["name"] for col in table["columns"]}
        for column in columns:
            if column not in available:
                raise InsufficientEvidence("column_unverified", f"{role} has no documented {column}")
            if table["parse_errors"].get(column):
                raise InsufficientEvidence("column_parse_errors", f"{role}.{column} has unresolved parse errors")
            if column not in nullable and table["null_counts"].get(column):
                raise InsufficientEvidence("column_missing_values", f"{role}.{column} contains missing values")
        self.columns[table["table"]] = list(dict.fromkeys([*self.columns.get(table["table"], []), *columns]))
        return table, quote(table["table"])

    def check(self, sql, description, expectation="zero"):
        self.checks.append({"sql": sql, "description": description, "expectation": expectation})

    def unique(self, table, key):
        self.check(f"SELECT COUNT(*) - COUNT(DISTINCT {key}) FROM {table}", f"Unique {key}")

    def raw_column(self, table, semantic, alias):
        source = next((c["source"] for c in table["columns"] if c["name"] == semantic), None)
        raw = next((name for name, label in table["raw_columns"].items() if label == source), None)
        if raw is None:
            raise InsufficientEvidence("column_unverified", f"No documented raw field for {semantic}")
        self.columns[table["table"]].append(raw)
        return f"{alias}.{quote(raw)}"

    def labels(self, table, column, name):
        labels = set(table.get("distinct_values", {}).get(column, []))
        labels.update(e["value"] for e in self.context["entities"]
                      if e["table"] == table["table"] and e["column"] == column)
        matching = [label for label in labels if norm(label, accents=True) == name]
        if not matching:
            raise InsufficientEvidence("entity_unverified", f"No exact documented label for {column}: {name}")
        return literals(matching)

    def dated_scope(self, table, column, selected, recognized, comparison=None):
        if not covered(table, column, comparison or self.intent.time, self.question.as_of_date, self.context["readme"]):
            raise InsufficientEvidence("coverage_unverified", f"Coverage unverified for {table['table']}.{column}")
        statuses = table.get("distinct_values", {}).get("status", [])
        unknown = [label for label in statuses if norm(label, accents=True) not in recognized]
        if not statuses or unknown:
            raise InsufficientEvidence("status_unverified", f"Unverified status labels: {unknown if statuses else 'no status evidence'}")
        allowed = [label for label in statuses if norm(label, accents=True) in selected]
        status = f"o.status IN ({literals(allowed)})" if allowed else "FALSE"
        prefix = "comparison_" if comparison else ""
        predicate = f"o.{column} >= CAST(${prefix}start AS DATE) AND o.{column} < CAST(${prefix}end AS DATE) AND {status}"
        if self.intent.shift:
            clause = self.rule("ca sang ca chieu", "theo gio tao don")
            label = "ca sáng" if self.intent.shift == "sang" else "ca chiều"
            window = re.search(label + r"\s+(\d\d):(\d\d)[–-](\d\d):(\d\d)", clause)
            if window is None:
                raise InsufficientEvidence("shift_unverified", "Rulebook does not give a supported shift window")
            sh, sm, eh, em = map(int, window.groups())
            predicate += f" AND (EXTRACT(HOUR FROM o.{column})*60+EXTRACT(MINUTE FROM o.{column})) BETWEEN {sh*60+sm} AND {eh*60+em}"
        return predicate

    def plan(self, *queries, output="number", decimals=0, postprocess="direct"):
        return Plan(route="sql", time=self.intent.time, tables=list(self.columns), required_columns=self.columns,
                    rule_refs=self.refs, queries=[{"sql": sql} for sql in queries], checks=self.checks,
                    comparison_time=self.intent.comparison, postprocess=postprocess,
                    output={"type": output, "decimals": decimals})

    def product(self, name, unit=None):
        product, table = self.table("products", "product_id", "product_name", "base_unit")
        self.unique(table, "product_id")
        predicate = f"product_name IN ({self.labels(product, 'product_name', name)})"
        if unit:
            predicate += f" AND base_unit IN ({self.labels(product, 'base_unit', unit)})"
        self.check(f"SELECT COUNT(*) = 1 FROM {table} WHERE {predicate}", "Unique documented product and unit", "true")
        return f"SELECT product_id FROM {table} WHERE {predicate}"

    def purchase(self):
        average = self.intent.kind == "purchase_average"
        if average:
            self.rule("gia nhap binh quan mot mat hang trong thang", "theo so luong nhap", "theo don vi nhap", "bo phieu huy")
        else:
            self.rule("tien nhap hang trong ky", "tong tien cac phieu nhap", "bo phieu huy")
        source, receipts = self.table("purchase_receipts", "receipt_id", "purchase_date", "status", "purchase_amount")
        scope = self.dated_scope(source, "purchase_date", PURCHASE_OK, PURCHASE_OK | CANCELLED)
        self.unique(receipts, "receipt_id")
        if not average:
            return self.plan(f"SELECT SUM(o.purchase_amount) FROM {receipts} o WHERE {scope}")
        lines, detail = self.table("purchase_lines", "receipt_id", "line_number", "product_id",
                                   "purchase_unit", "purchase_quantity")
        product = self.product(self.intent.entity)
        units = self.labels(lines, "purchase_unit", self.intent.unit)
        base = f"FROM {receipts} o JOIN {detail} l ON o.receipt_id=l.receipt_id WHERE {scope} AND l.product_id IN ({product})"
        price = self.purchase_price(lines, receipts, detail, scope, product)
        # A mixed-unit period needs an explicitly validated conversion.
        self.check(f"SELECT COUNT(*) {base} AND l.purchase_unit NOT IN ({units})", "Purchase lines use the requested unit")
        self.check(f"SELECT COUNT(*) FROM (SELECT l.receipt_id, l.line_number {base} "
                   "GROUP BY l.receipt_id, l.line_number HAVING COUNT(*) > 1) conflicting_lines",
                   "Unique purchase line IDs")
        return self.plan(f"SELECT SUM(l.purchase_quantity*{price}) {base}",
                         f"SELECT SUM(l.purchase_quantity) {base}", postprocess="average_money")

    def purchase_price(self, lines, receipts, detail, scope, product):
        name = "purchase_unit_price"
        required = self.columns[lines["table"]]
        if not lines["parse_errors"].get(name) and not lines["null_counts"].get(name):
            required.append(name)
            return f"l.{name}"
        price = grouped_currency_sql(self.raw_column(lines, name, "l"))
        # The grouping interpretation is only usable if all selected receipt totals reconcile.
        self.check(
            f"SELECT COUNT(*) FROM (SELECT o.receipt_id FROM {receipts} o JOIN {detail} l "
            f"ON l.receipt_id=o.receipt_id WHERE {scope} AND EXISTS "
            f"(SELECT 1 FROM {detail} target WHERE target.receipt_id=o.receipt_id AND target.product_id IN ({product})) "
            f"GROUP BY o.receipt_id, o.purchase_amount HAVING COUNT({price}) <> COUNT(*) "
            f"OR SUM(l.purchase_quantity*{price}) IS DISTINCT FROM o.purchase_amount) mismatched_receipts",
            "Formatted unit prices reconcile with documented receipt totals",
        )
        return price

    def snapshot(self):
        if snapshot_day(self.context["readme"]) != self.question.as_of_date.isoformat():
            raise InsufficientEvidence("snapshot_date_unverified", "Requested date differs from the documented snapshot date")
        self.rule("khong co du lieu khac voi bang 0")
        source, table = self.table("snapshot", "product_id", "product_name", "base_unit", "snapshot_quantity")
        names = self.labels(source, "product_name", self.intent.entity)
        units = self.labels(source, "base_unit", self.intent.unit)
        where = f"WHERE product_name IN ({names}) AND base_unit IN ({units})"
        self.check(f"SELECT COUNT(*) = 1 FROM {table} {where}", "Unique stock snapshot row", "true")
        return self.plan(f"SELECT snapshot_quantity FROM {table} {where}", decimals=None)

    def sales(self):
        kind = self.intent.kind
        self.rule("don thuoc ky nao", "theo ngay dat")
        if kind in ("cancelled", "cancel_rate"):
            self.rule("don bi huy", "don co trang thai huy")
        elif kind == "unit_price":
            self.rule("gia ban x ngay d", "don gia ghi tren dong ban", "tinh ca dong cua don huy don tra")
        else:
            self.rule("don tinh vao doanh thu", "khong huy", "khong tra hang", "dang giao van tinh")
        customer_query = kind in ("customer_share", "customer_revenue", "top_customer", "top_customers")
        order_columns = ["customer_id"] if customer_query else []
        available_roles = {t.get("documented_role") for t in self.context["catalog"]}
        role = "sales_flat" if "sales_flat" in available_roles and "sales_orders" not in available_roles else "sales_orders"
        if "sales_flat" in available_roles and "sales_orders" in available_roles:
            raise InsufficientEvidence("schema_ambiguous", "Both flat and separate sales exports exist")
        if role == "sales_flat" and "dong so luong am la don co tra hang" in norm(self.context["readme"], accents=True):
            raise InsufficientEvidence("return_evidence_required", "Negative-quantity returns require the general planner to link the original order")
        source, orders = self.table(role, "order_id", "status", *order_columns, nullable=("customer_id",))
        available = {col["name"] for col in source["columns"]}
        date_column = "created_at" if "created_at" in available else "order_date"
        if self.intent.shift and date_column != "created_at":
            raise InsufficientEvidence("shift_unverified", "Only a date column is documented")
        self.table(role, date_column)
        order_fields = ", ".join(["order_id", date_column, "status", *order_columns])
        order_rows = f"(SELECT DISTINCT {order_fields} FROM {orders})" if role == "sales_flat" else orders
        active = SALE_OK
        if any("moi don co dung mot trong bon trang thai" in norm(clause["text"], accents=True)
               for clause in self.rules.clauses.values()):
            active = active | {"dang giao dich", "dang xu ly"}
        recognized = active | CANCELLED | RETURNED
        selected = CANCELLED if kind in ("cancelled", "cancel_rate") else recognized if kind == "unit_price" else active
        scope = self.dated_scope(source, date_column, selected, recognized)
        self.unique(order_rows, "order_id")
        if kind == "cancel_rate":
            self.rule("ti le don huy trong ky", "moi don dat trong ky")
            denominator = self.dated_scope(source, date_column, recognized, recognized)
            return self.plan(f"SELECT COUNT(*) FROM {order_rows} o WHERE {scope}",
                             f"SELECT COUNT(*) FROM {order_rows} o WHERE {denominator}",
                             output="percent", decimals=2, postprocess="ratio_percent")
        if kind in ("orders", "cancelled"):
            if kind == "orders":
                self.rule("so don", "khong huy khong tra")
            return self.plan(f"SELECT COUNT(*) FROM {order_rows} o WHERE {scope}")
        quantity = kind in ("quantity", "top_products")
        if not quantity and kind != "unit_price":
            self.rule("doanh thu mot don", "thanh tien cac dong hang", "da tru giam gia tren tung dong")
        fields = ["product_id", "quantity_base"] if quantity else ["line_amount"]
        if kind == "unit_price":
            fields = ["product_id", "quantity_selling", "quantity_base"]
        if kind in ("product_revenue", "category_revenue"):
            fields.append("product_id")
        if kind == "top_products":
            fields.append("product_name")
        line_role = "sales_flat" if role == "sales_flat" else "sales_lines"
        line_source, lines = self.table(line_role, "order_id", *fields)
        has_line_key = any(c["name"] == "line_number" for c in line_source["columns"])
        if has_line_key:
            self.table(line_role, "line_number")
        elif not line_source.get("deduplicates_order_rows"):
            raise InsufficientEvidence("line_identity_unverified", "No line key or documented export duplicate rule")

        def lines_in_scope(predicate):
            base = f"FROM {order_rows} o JOIN {lines} l ON l.order_id=o.order_id WHERE {predicate}"
            if role != "sales_flat":
                self.check(f"SELECT COUNT(*) FROM {order_rows} o WHERE {predicate} AND NOT EXISTS "
                           f"(SELECT 1 FROM {lines} l WHERE l.order_id=o.order_id)", "Every included order has lines")
            if has_line_key:
                self.check(f"SELECT COUNT(*) FROM (SELECT l.order_id, l.line_number {base} "
                           "GROUP BY l.order_id, l.line_number HAVING COUNT(*) > 1) conflicting_lines",
                           "Unique sales line IDs")
            return base

        base = lines_in_scope(scope)
        if kind == "sales":
            return self.plan(f"SELECT SUM(l.line_amount) {base}")
        if kind == "unit_price":
            product = self.product(self.intent.entity, self.intent.unit)
            filtered = f"{base} AND l.product_id IN ({product})"
            if line_source["parse_errors"].get("unit_price") or line_source["null_counts"].get("unit_price"):
                self.table(line_role, "line_amount")
                raw_price = self.raw_column(line_source, "unit_price", "l")
                raw_discount = self.raw_column(line_source, "line_discount", "l")
                price = grouped_currency_sql(raw_price)
                self.check(
                    f"SELECT COUNT(*) {filtered} AND (TRY_CAST(TRIM({raw_discount}) AS DECIMAL(38,8)) "
                    f"IS DISTINCT FROM 0 OR {price}*l.quantity_selling IS DISTINCT FROM l.line_amount)",
                    "Grouped unit prices reconcile with undiscounted line amounts",
                )
            else:
                self.table(line_role, "unit_price")
                price = "l.unit_price"
            value = f"ROUND(({price})*l.quantity_selling/NULLIF(l.quantity_base,0),8)"
            self.check(f"SELECT COUNT(*) {filtered} AND (l.quantity_selling <= 0 OR l.quantity_base <= 0)",
                       "Positive quantities establish unit conversion")
            self.check(f"SELECT COUNT(DISTINCT {value}) = 1 {filtered}", "Exactly one recorded base-unit price", "true")
            return self.plan(f"SELECT MIN({value}) {filtered}")
        if kind == "growth":
            self.rule("phan tram thay doi tang truong", "so voi ky truoc")
            previous = lines_in_scope(self.dated_scope(source, date_column, active, recognized, self.intent.comparison))
            return self.plan(f"SELECT SUM(l.line_amount) {base}", f"SELECT SUM(l.line_amount) {previous}",
                             output="percent", decimals=2, postprocess="growth_percent")
        if kind == "product_revenue":
            product = self.product(self.intent.entity)
            return self.plan(f"SELECT SUM(l.line_amount) {base} AND l.product_id IN ({product})")
        if kind == "category_revenue":
            product, products = self.table("products", "product_id", "product_category")
            self.unique(products, "product_id")
            categories = self.labels(product, "product_category", self.intent.entity)
            return self.plan(f"SELECT SUM(l.line_amount) {base} AND l.product_id IN "
                             f"(SELECT product_id FROM {products} WHERE product_category IN ({categories}))")
        if kind == "average":
            self.rule("gia tri trung binh mot don", "lam tron ve dong")
            return self.plan(f"SELECT SUM(l.line_amount) {base}", f"SELECT COUNT(DISTINCT o.order_id) {base}",
                             postprocess="average_money")
        if kind == "peak_day":
            return self.plan(f"SELECT CAST(o.{date_column} AS DATE) {base} GROUP BY 1 "
                             "ORDER BY SUM(l.line_amount) DESC, 1 ASC LIMIT 1", output="date")
        if kind == "quantity":
            self.rule("ban duoc bao nhieu don vi", "so luong quy ve don vi co ban")
            product = self.product(self.intent.entity, self.intent.unit)
            return self.plan(f"SELECT SUM(l.quantity_base) {base} AND l.product_id IN ({product})", decimals=None)
        if kind == "top_products":
            self.rule("ban chay", "so luong quy ve don vi co ban")
            self.rule("mat hang", "phi phuc vu khong phai mat hang")
            _, products = self.table("products", "product_id", "product_name")
            self.unique(products, "product_id")
            base += " AND LOWER(TRIM(l.product_name)) <> 'phí phục vụ'"
            self.check(f"SELECT COUNT(*) {base} AND NOT EXISTS "
                       f"(SELECT 1 FROM {products} p WHERE p.product_id=l.product_id)", "Ranked products have documented names")
            return self.plan(f"SELECT p.product_name FROM {products} p JOIN "
                             f"(SELECT l.product_id, SUM(l.quantity_base) AS quantity {base} GROUP BY l.product_id) r "
                             f"ON p.product_id=r.product_id ORDER BY r.quantity DESC, p.product_id ASC LIMIT {self.intent.limit}",
                             output="list", decimals=None)
        return self.customers(base)

    def customers(self, base):
        group_query = self.intent.kind in ("customer_share", "customer_revenue")
        field = "customer_group" if group_query else "customer_name"
        source, customers = self.table("customers", "customer_id", field, nullable=("customer_group",))
        self.unique(customers, "customer_id")
        known = "o.customer_id IS NOT NULL"
        self.check(f"SELECT COUNT(*) {base} AND {known} AND NOT EXISTS "
                   f"(SELECT 1 FROM {customers} c WHERE c.customer_id=o.customer_id)", "Named customers are in the customer master")
        if group_query:
            self.rule("doanh thu theo nhom khach", "ca shop", "vang lai")
            groups = self.labels(source, "customer_group", self.intent.entity)
            numerator = (f"SELECT COALESCE(SUM(l.line_amount), 0) {base} AND EXISTS "
                         f"(SELECT 1 FROM {customers} c WHERE c.customer_id=o.customer_id AND c.customer_group IN ({groups}))")
            if self.intent.kind == "customer_revenue":
                return self.plan(numerator)
            return self.plan(numerator, f"SELECT SUM(l.line_amount) {base}", output="percent", decimals=2,
                             postprocess="ratio_percent")
        self.rule("xac dinh khach", "khach mua nhieu tien", "doanh thu")
        return self.plan(f"SELECT c.customer_name FROM {customers} c JOIN "
                         f"(SELECT o.customer_id, SUM(l.line_amount) AS revenue {base} AND {known} GROUP BY o.customer_id) r "
                         f"ON c.customer_id=r.customer_id ORDER BY r.revenue DESC, c.customer_id ASC LIMIT {self.intent.limit}",
                         output="list" if self.intent.kind == "top_customers" else "string", decimals=None)


def assess_verified_query(question, context, rules):
    intent = parse_intent(question.question)
    if intent is None:
        return None, {"code": "intent_not_supported", "detail": "Question needs the general planner"}
    query = VerifiedQuery(question, intent, context, rules)
    try:
        if intent.kind in ("purchase", "purchase_average"):
            plan = query.purchase()
        elif intent.kind == "snapshot":
            plan = query.snapshot()
        else:
            plan = query.sales()
    except InsufficientEvidence as exc:
        return None, {"code": exc.code, "detail": str(exc)}
    return plan, None

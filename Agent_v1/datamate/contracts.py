from __future__ import annotations
from datetime import date
from typing import Literal, Any
from pydantic import Field, model_validator
from .config import Strict
from .util import safe_id


class Question(Strict):
    id: str
    business_id: str
    question: str
    as_of_date: date

    @model_validator(mode="after")
    def ids(self):
        safe_id(self.id); safe_id(self.business_id)
        return self


class ColumnMapping(Strict):
    source: str
    semantic: str
    kind: Literal["text", "number", "date", "datetime"] = "text"
    decimal_separator: Literal["auto", ".", ","] = "auto"
    evidence: str = ""


class TableMapping(Strict):
    role: str = "unknown"
    grain: str = "unknown"
    header_row: int = Field(1, ge=1, le=30)
    columns: list[ColumnMapping] = Field(default_factory=list)
    group_key: str | None = None
    fill_columns: list[str] = Field(default_factory=list)
    deduplicate_within_order: bool = False
    coverage_start: str | None = None
    coverage_end: str | None = None
    coverage_evidence: str = ""
    notes: list[str] = Field(default_factory=list)


class TimeSpec(Strict):
    kind: Literal["all", "today", "yesterday", "this_week", "previous_week", "this_month", "previous_month", "previous_month_to_date", "year_to_date", "last_n_days", "month", "quarter", "range", "as_of", "tet"] = "all"
    year: int | None = None
    month: int | None = None
    quarter: int | None = None
    n: int | None = None
    start: str | None = None
    end: str | None = None


class QuerySpec(Strict):
    sql: str
    purpose: str


class CheckSpec(Strict):
    sql: str
    description: str
    expectation: Literal["zero", "positive", "true"] = "zero"


class OutputSpec(Strict):
    type: Literal["number", "percent", "string", "date", "boolean", "list", "table"]
    decimals: int | None = Field(None, ge=0, le=8)
    columns: list[str] = Field(default_factory=list)
    column_decimals: list[int | None] = Field(default_factory=list)
    item_decimals: list[int | None] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_precision(self):
        if any(p is not None and not 0 <= p <= 8 for p in self.column_decimals + self.item_decimals):
            raise ValueError("Per-value precision must be between 0 and 8")
        return self


class Plan(Strict):
    route: Literal["metric", "sql", "unanswerable", "abstain"] = "sql"
    metric: Literal["sum", "count", "distinct_count", "average", "rank", "group_sum"] = "sum"
    time: TimeSpec = Field(default_factory=TimeSpec)
    comparison_time: TimeSpec | None = None
    queries: list[QuerySpec] = Field(default_factory=list)
    checks: list[CheckSpec] = Field(default_factory=list)
    postprocess: Literal["direct", "ratio_percent", "growth_percent", "difference", "list_scalars", "average_money"] = "direct"
    output: OutputSpec = Field(default_factory=lambda: OutputSpec(type="number"))
    limit: int = Field(1, ge=1, le=1000)
    rule_refs: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    required_columns: dict[str, list[str]] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list)
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)
    ambiguity: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def shape(self):
        if self.route in ("metric", "sql") and not self.queries:
            raise ValueError("Answer plans require executable queries")
        if self.route == "metric" and len(self.queries) != 1:
            raise ValueError("Metric route requires one normalized source query")
        if self.postprocess in ("ratio_percent", "growth_percent", "difference", "average_money") and len(self.queries) != 2:
            raise ValueError("This postprocessor requires two scalar queries")
        if self.route == "unanswerable" and not self.evidence:
            raise ValueError("Unanswerable requires concrete source evidence")
        return self


class Review(Strict):
    verdict: Literal["accept", "repair", "abstain"]
    issues: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class RuleItem(Strict):
    name: str
    clause_id: str
    interpretation: str


class RuleIndex(Strict):
    rules: list[RuleItem] = Field(default_factory=list)


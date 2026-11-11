import calendar as cal
import csv
from datetime import date, timedelta
from .contracts import TimeSpec
from .normalize import parse_date


def previous_month(d):
    return (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)


def resolve(spec: TimeSpec, as_of: date, calendar_path=None):
    """Returns inclusive start, exclusive end; None means unbounded."""
    k = spec.kind
    if k == "all":
        return None, None
    if k == "as_of":
        return None, (as_of + timedelta(days=1)).isoformat()
    if k in ("today", "yesterday"):
        start = as_of - timedelta(days=k == "yesterday")
        end = start + timedelta(days=1)
    elif k in ("this_week", "previous_week"):
        monday = as_of - timedelta(days=as_of.weekday())
        start, end = (monday, as_of + timedelta(days=1)) if k == "this_week" else (monday - timedelta(days=7), monday)
    elif k == "this_month":
        start, end = as_of.replace(day=1), as_of + timedelta(days=1)
    elif k in ("previous_month", "previous_month_to_date"):
        y, m = previous_month(as_of)
        start = date(y, m, 1)
        last = cal.monthrange(y, m)[1] if k == "previous_month" else min(as_of.day, cal.monthrange(y, m)[1])
        end = date(y, m, last) + timedelta(days=1)
    elif k == "year_to_date":
        start, end = date(as_of.year, 1, 1), as_of + timedelta(days=1)
    elif k == "last_n_days":
        if not spec.n or spec.n < 1:
            raise ValueError("last_n_days requires positive n")
        start, end = as_of - timedelta(days=spec.n - 1), as_of + timedelta(days=1)
    elif k in ("month", "quarter"):
        m = spec.month if k == "month" else ((spec.quarter or 0) - 1) * 3 + 1
        if not m or not 1 <= m <= 12:
            raise ValueError("Invalid month/quarter")
        y = spec.year or (as_of.year if date(as_of.year, m, 1) <= as_of else as_of.year - 1)
        start = date(y, m, 1)
        final_m = m if k == "month" else m + 2
        end = date(y, final_m, cal.monthrange(y, final_m)[1]) + timedelta(days=1)
    elif k == "range":
        if not (spec.start and spec.start.strip() and spec.end and spec.end.strip()):
            raise ValueError("range requires inclusive ISO start and end dates")
        start = date.fromisoformat(parse_date(spec.start))
        end = date.fromisoformat(parse_date(spec.end)) + timedelta(days=1)
    elif k == "tet":
        y = spec.year or as_of.year
        if calendar_path is None:
            raise ValueError("Tết requires supplied calendar")
        start = end = None
        with open(calendar_path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("lunar_leap", "false").lower() in ("true", "1"):
                    continue
                lunar = tuple(int(row[n]) for n in ("lunar_year", "lunar_month", "lunar_day"))
                if lunar == (y - 1, 12, 23):
                    start = date.fromisoformat(row["date"])
                if lunar == (y, 1, 10):
                    end = date.fromisoformat(row["date"]) + timedelta(days=1)
        if start is None or end is None:
            raise ValueError("Supplied calendar does not cover this Tết")
    else:
        raise ValueError(k)
    if start >= end:
        raise ValueError("Empty or reversed period")
    return start.isoformat(), end.isoformat()


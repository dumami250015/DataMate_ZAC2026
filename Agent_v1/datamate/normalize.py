"""Conservative parsers: ambiguous values remain errors, never zero."""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from .util import norm


def number(value, separator="auto"):
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a numeric measure")
    if isinstance(value, (int, float, Decimal)):
        result = Decimal(str(value))
    else:
        s = str(value).strip().replace("\u00a0", " ")
        negative = s.startswith("(") and s.endswith(")")
        if negative:
            s = s[1:-1]
        match = re.fullmatch(r"\s*([+-]?[\d][\d.,\s]*)(?:\s*[^\d.,]+)?\s*", s)
        if not match:
            raise ValueError(f"Invalid number {value!r}")
        s = re.sub(r"\s", "", match.group(1))
        if separator == "auto":
            if "." in s and "," in s:
                sep = "." if s.rfind(".") > s.rfind(",") else ","
                s = s.replace("," if sep == "." else ".", "").replace(sep, ".")
            elif "." in s or "," in s:
                sep = "." if "." in s else ","
                pieces = s.lstrip("+-").split(sep)
                if all(len(p) == 3 for p in pieces[1:]):
                    # One separator + three digits could be either convention.
                    if len(pieces) == 2:
                        raise ValueError(f"Ambiguous separator in {value!r}; specify column convention")
                    s = s.replace(sep, "")
                elif len(pieces) == 2:
                    s = s.replace(sep, ".")
                else:
                    raise ValueError(f"Ambiguous grouping in {value!r}")
        else:
            s = s.replace("," if separator == "." else ".", "").replace(separator, ".")
        try:
            result = Decimal(s)
        except InvalidOperation as e:
            raise ValueError(f"Invalid number {value!r}") from e
        if negative:
            result = -result
    if not result.is_finite():
        raise ValueError("Non-finite number")
    return result


def parse_date(value, year=None, timestamp=False):
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time())
    else:
        s = str(value).strip()
        s = re.sub(r"(\d{1,2})\s*thg\s*(\d{1,2})\s*(\d{4})", r"\1/\2/\3", s, flags=re.I)
        result = None
        try:
            result = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            pass
        if result is None:
            for fmt in ["%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%Y/%m/%d", "%d.%m.%Y"]:
                try:
                    result = datetime.strptime(s, fmt); break
                except ValueError:
                    pass
        if result is None and year and re.fullmatch(r"\d{1,2}[/.-]\d{1,2}", s):
            d, m = map(int, re.split(r"[/.-]", s))
            result = datetime(year, m, d)
        if result is None:
            raise ValueError(f"Unresolved date {value!r}")
    if result.tzinfo:
        raise ValueError("Timezone-aware source requires explicit business timezone mapping")
    return result.isoformat(sep=" ") if timestamp else result.date().isoformat()


def year_from_sheet(sheet):
    matches = re.findall(r"(?<!\d)(20\d{2})(?!\d)", sheet)
    return int(matches[-1]) if matches else None


def infer_kind(header, samples):
    """Metadata-only hints; not a semantic mapping or availability guarantee."""
    h = norm(header, accents=True)
    if any(w in h.split() for w in ["ngay", "date", "thoigian"]):
        return "date"
    return "text"


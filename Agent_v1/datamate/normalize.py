"""Conservative parsers: ambiguous values remain errors, never zero."""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from .util import norm


NUMBER_TEXT = re.compile(r"\s*([+-]?[\d][\d.,\s]*)(?:\s*[^\d.,]+)?\s*")


def numeric_text(value):
    text = value.strip().replace("\u00a0", " ")
    negative = text.startswith("(") and text.endswith(")")
    match = NUMBER_TEXT.fullmatch(text[1:-1] if negative else text)
    if not match:
        raise ValueError(f"Invalid number {value!r}")
    return re.sub(r"\s", "", match.group(1)), negative


def infer_decimal_separator(samples, proposed):
    """Use unambiguous formatting evidence before interpreting single separators."""
    evidence = set()
    for value in samples:
        if not isinstance(value, str):
            continue
        try:
            text, _ = numeric_text(value)
        except ValueError:
            continue
        if "." in text and "," in text:
            separator = "." if text.rfind(".") > text.rfind(",") else ","
        else:
            mark = "." if "." in text else ","
            parts = text.split(mark)
            if len(parts) == 1 or (len(parts) == 2 and len(parts[-1]) == 3):
                continue
            separator = ("," if mark == "." else ".") if len(parts) > 2 else mark
        try:
            number(value, separator)
        except ValueError:
            continue
        evidence.add(separator)
    if len(evidence) == 1:
        return evidence.pop()
    return "auto" if evidence else proposed


def number(value, separator="auto"):
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a numeric measure")
    if isinstance(value, (int, float, Decimal)):
        result = Decimal(str(value))
    else:
        s, negative = numeric_text(str(value))
        if separator == "auto":
            if "." in s and "," in s:
                separator = "." if s.rfind(".") > s.rfind(",") else ","
            elif "." in s or "," in s:
                sep = "." if "." in s else ","
                pieces = s.lstrip("+-").split(sep)
                if all(len(p) == 3 for p in pieces[1:]):
                    # One separator + three digits could be either convention.
                    if len(pieces) == 2:
                        raise ValueError(f"Ambiguous separator in {value!r}; specify column convention")
                    separator = "," if sep == "." else "."
                elif len(pieces) == 2:
                    separator = sep
                else:
                    raise ValueError(f"Ambiguous grouping in {value!r}")
        if separator != "auto":
            grouping = "," if separator == "." else "."
            integer, _, fraction = s.partition(separator)
            groups = integer.lstrip("+-").split(grouping)
            if grouping in fraction or (len(groups) > 1 and (
                not 1 <= len(groups[0]) <= 3 or any(len(part) != 3 for part in groups[1:])
            )):
                raise ValueError(f"Invalid digit grouping in {value!r}")
            s = s.replace(grouping, "").replace(separator, ".")
        try:
            result = Decimal(s)
        except InvalidOperation as e:
            raise ValueError(f"Invalid number {value!r}") from e
        if negative:
            result = -result
    if not result.is_finite():
        raise ValueError("Non-finite number")
    return result


def money(value, separator="auto"):
    if isinstance(value, str):
        text = value.strip().casefold()
        shorthand = re.fullmatch(r"([+-]?\d+)\s*tr\s*(\d{1,6})(?:\s*(?:đ|₫|vnd))?", text)
        if shorthand:
            millions, fraction = shorthand.groups()
            amount = abs(int(millions)) * 1_000_000 + int(fraction.ljust(6, "0"))
            return Decimal(-amount if millions.startswith("-") else amount)
        suffix = re.fullmatch(r"([+-]?[\d.,\s]+?)\s*(k|tr|triệu|nghìn|ngàn)(?:\s*(?:đ|₫|vnd))?", text)
        if suffix:
            mantissa, unit = suffix.groups()
            multiplier = 1_000 if unit in ("k", "nghìn", "ngàn") else 1_000_000
            return number(mantissa, separator) * multiplier
        amount_text = text[1:-1] if text.startswith("(") and text.endswith(")") else text
        if text and not re.fullmatch(r"[+-]?[\d.,\s]+(?:đ|₫|vnd)?", amount_text):
            raise ValueError(f"Unrecognized currency format {value!r}")
    return number(value, separator)


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
            for fmt in ["%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y",
                        "%d/%m/%y", "%d-%m-%y", "%d.%m.%y", "%Y/%m/%d", "%d.%m.%Y"]:
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


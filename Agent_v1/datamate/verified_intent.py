"""Recognize bounded analytical requests without guessing omitted qualifiers."""
import re
from dataclasses import dataclass
from datetime import date

from .contracts import TimeSpec
from .util import norm


PERIODS = {
    "hom nay": "today", "hom qua": "yesterday",
    "tuan nay": "this_week", "tuan truoc": "previous_week", "tuan roi": "previous_week",
    "thang nay": "this_month", "thang truoc": "previous_month",
}
SPELLINGS = {"dthu": "doanh thu", "nhiu": "nhieu", "nhju": "nhieu", "hnay": "hom nay",
             "hqua": "hom qua", "dc": "duoc", "cuwar": "cua",
             "bn": "bao nhieu", "bnhiu": "bao nhieu", "toongr": "tong",
             "giumf": "gium", "trc": "truoc"}
MONTH = r"thang (?P<month>0?[1-9]|1[0-2]) (?:nam )?(?P<year>20\d{2})"
DAY = r"ngay (?P<day_iso>20\d{2} \d{2} \d{2})"
QUARTER = r"quy (?P<quarter>[1-4]) (?:nam )?(?P<quarter_year>20\d{2})"
PERIOD = re.compile(r"\b(?:trong )?(?:(?:ngay )?(?P<relative>" + "|".join(PERIODS) + ")|" + DAY + "|" + QUARTER + "|" + MONTH + r")\b")
SHIFT = re.compile(r"\b(?:(?:rieng|trong) )?ca (sang|chieu)\b")


@dataclass(frozen=True)
class Intent:
    kind: str
    time: TimeSpec
    shift: str | None = None
    entity: str = ""
    unit: str = ""
    limit: int = 1
    comparison: TimeSpec | None = None


def without_match(text, match):
    return " ".join((text[:match.start()] + " " + text[match.end():]).split())


def normalized_question(question):
    text = question
    text = re.sub(r"(?i)(?<![\d/])(?:(?:hôm|hom|ngày|ngay)\s+)?(\d{1,2})/(\d{1,2})/(20\d{2})(?![\d/])",
                  lambda m: f"ngay {m[3]} {int(m[2]):02d} {int(m[1]):02d}", text)
    text = re.sub(r"(?i)(?<![\w/])(?:(?:tháng|thang|th|t)\s*)?(0?[1-9]|1[0-2])/(20\d{2}|\d{2})(?![\d/])",
                  lambda m: f"thang {m[1]} {m[2] if len(m[2]) == 4 else '20' + m[2]}", text)
    text = " ".join(SPELLINGS.get(word, word) for word in norm(text, accents=True).split())
    text = re.sub(r"\b(?:bua nay|hom ni)\b", "hom nay", text)
    return re.sub(r"^(?:cho hoi|cho minh hoi|cho toi biet|xem giup|coi gium|check gium|rieng ngay) |(?: (?:vay|vaayj|a|nhi|do|z|roi|ne))+$", "", text)


def time_spec(match):
    if match["day_iso"]:
        value = date.fromisoformat(match["day_iso"].replace(" ", "-")).isoformat()
        return TimeSpec(kind="range", start=value, end=value)
    if match["quarter"]:
        return TimeSpec(kind="quarter", year=int(match["quarter_year"]), quarter=int(match["quarter"]))
    if match["month"]:
        return TimeSpec(kind="month", year=int(match["year"]), month=int(match["month"]))
    return TimeSpec(kind=PERIODS[match["relative"]])


def explicit_question_time(question):
    text = normalized_question(question)
    periods = list(PERIOD.finditer(text))
    if len(periods) == 1 and not periods[0]["relative"] and not re.search(r"\b(?:so voi|tu|den|cung ky)\b", text):
        try:
            return time_spec(periods[0])
        except ValueError:
            return None
    return None


def parse_intent(question):
    text = normalized_question(question)
    snapshot = re.fullmatch(r"(?:e |em )?tinh den (?:cuoi|het) ngay hom nay trong kho con (?:may|bao nhieu) (\S+) (.+)", text)
    if snapshot:
        return Intent("snapshot", TimeSpec(kind="as_of"), entity=snapshot[2], unit=snapshot[1])
    snapshot = re.fullmatch(r"ton kho (.+) (?:cuoi|het) ngay hom nay (?:la |con )?(?:bao nhieu|may) (\S+)", text)
    if snapshot:
        return Intent("snapshot", TimeSpec(kind="as_of"), entity=snapshot[1], unit=snapshot[2])
    comparison = re.search(r"\b(?:so(?: voi)? )?(?:cung ky )?(thang (?:lien )?truoc|nam truoc)\b", text)
    comparison_text = None
    if comparison and "doanh thu" in text and re.search(r"\b(?:phan tram|hon hay kem|thay doi|tang giam)\b", text):
        comparison_text = comparison[1]
        text = without_match(text, comparison)
    periods = list(PERIOD.finditer(text))
    if len(periods) != 1:
        return None
    period = periods[0]
    try:
        time = time_spec(period)
    except ValueError:
        return None
    text = without_match(text, period)
    shifts = list(SHIFT.finditer(text))
    if len(shifts) > 1:
        return None
    shift = shifts[0][1] if shifts else None
    if shifts:
        text = without_match(text, shifts[0])
    if comparison_text and time.kind == "month" and re.fullmatch(
        r"(?:tong )?doanh thu (?:hon hay kem|thay doi|tang giam) (?:bao nhieu|may)(?: phan tram)?", text,
    ):
        year, month = time.year, time.month
        if comparison_text == "nam truoc":
            year -= 1
        else:
            year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        return Intent("growth", time, comparison=TimeSpec(kind="month", year=year, month=month))
    if comparison_text:
        return None
    if re.fullmatch(r"(?:(?:tong )?doanh thu(?: (?:duoc|quan|tiem))?(?: (?:la )?bao nhieu(?: tien)?)?|(?:cua hang |tiem |quan )?ban duoc(?: tong cong)? (?:la )?bao nhieu tien|(?:tong )?tien ban (?:la )?bao nhieu)", text):
        return Intent("sales", time, shift)
    if re.fullmatch(r"(?:(?:cua hang |tiem |quan )?(?:ban )?duoc (?:may|bao nhieu) don(?: hang)?|(?:tiem |quan |cua hang )?co (?:tong cong )?bao nhieu don(?: hang)?|dem so don ban)", text):
        return Intent("orders", time, shift)
    if shift:
        return None
    patterns = {
        "purchase": r"tong (?:(?:gia tri|tien|so tien) (?:cac )?phieu nhap(?: hang)?|tien hang nhap vao) (?:la )?bao nhieu(?: tien)?",
        "cancelled": r"(?:so don (?:bi )?huy (?:la )?bao nhieu|bi huy (?:may|bao nhieu) don)",
        "cancel_rate": r"ti le (?:huy don|don huy) (?:la )?(?:bao nhieu|may)(?: phan tram)?",
        "average": r"(?:trung binh (?:moi|1) don (?:mang ve|dem ve) bao nhieu doanh thu|gia tri trung binh mot don (?:la )?bao nhieu)",
        "peak_day": r"(?:doanh thu ngay nao cao nhat|ngay nao cua hang ban duoc nhieu tien nhat)",
        "top_customer": r"khach mua nhieu tien nhat la ai",
        "customer_share": r"trong tong doanh thu phan cua khach nhom (.+) la (?:may|bao nhieu)",
        "quantity": r"mat hang (.+) ban duoc(?: tong cong)? bao nhieu (\S+)",
        "purchase_average": r"gia nhap binh quan mot (\S+) (.+) la bao nhieu",
        "top_products": r"(\d+) (?:best seller|mat hang ban chay nhat) la nhung mat hang nao",
        "top_customers": r"top (\d+) khach hang mua nhieu tien nhat la ai",
        "customer_revenue": r"tinh doanh thu cac don do khach nhom (.+) mua",
        "category_revenue": r"nhom (.+) ban duoc bao nhieu tien",
        "product_revenue": r"ban (.+) duoc bao nhieu tien",
        "quantity_reverse": r"ban ra bao nhieu (\S+) (.+)",
        "purchase_average_reverse": r"cua hang nhap (.+) binh quan bao nhieu tien mot (\S+)",
        "unit_price": r"(.+) ban ra voi don gia bao nhieu tien mot (\S+)",
    }
    for kind, pattern in patterns.items():
        match = re.fullmatch(pattern, text)
        if not match:
            continue
        if kind in ("customer_share", "customer_revenue", "category_revenue", "product_revenue"):
            return Intent(kind, time, entity=match[1])
        if kind == "quantity":
            return Intent(kind, time, entity=match[1], unit=match[2])
        if kind == "purchase_average":
            return Intent(kind, time, entity=match[2], unit=match[1])
        if kind == "quantity_reverse":
            return Intent("quantity", time, entity=match[2], unit=match[1])
        if kind == "purchase_average_reverse":
            return Intent("purchase_average", time, entity=match[1], unit=match[2])
        if kind == "unit_price":
            return Intent(kind, time, entity=match[1], unit=match[2]) if time.kind == "range" else None
        if kind in ("top_products", "top_customers"):
            limit = int(match[1])
            return Intent(kind, time, limit=limit) if 1 <= limit <= 100 else None
        return Intent(kind, time)
    return None

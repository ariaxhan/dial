"""Turning a bank or card statement export into charges the detector can reason about.

Most of the work here is not parsing CSV, it is figuring out that

    SQ *ANYTIME FITNESS 4471    POS DEBIT ANYTIME FIT #221    ANYTIME FITNESS INC

are one vendor. Statement descriptors are written for reconciliation, not for people, and a
detector that treats every descriptor variant as a separate vendor will never see a cadence
and will therefore never find anything.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from pathlib import Path

from dial.leaks import Charge

__all__ = ["normalize_vendor", "guess_category", "parse_csv", "parse_csv_file"]

#: Payment processor and channel noise that prefixes a real merchant name.
_PREFIXES = (
    "sq *", "tst*", "tst *", "sp *", "pp*", "pp *", "paypal *", "paypal-",
    "pos debit", "pos purchase", "debit card purchase", "recurring debit card",
    "recurring payment", "purchase authorized on", "ach debit", "ach payment",
    "web payment", "card payment", "visa purchase", "checkcard",
)

#: Trailing noise: store numbers, phone numbers, city and state, reference ids.
_TRAILING = (
    re.compile(r"\s+#\s*\d+\s*$"),
    re.compile(r"\s+\d{3}[- ]\d{3}[- ]\d{4}\s*$"),      # a phone number
    re.compile(r"\s+[A-Z]{2}\s*$"),                       # a state code
    re.compile(r"\s+\d{4,}\s*$"),                         # a long reference number
    re.compile(r"\s+(inc|llc|ltd|co|corp|company)\.?\s*$", re.I),
)

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%b %d, %Y", "%Y/%m/%d")

#: Vendor to category. Category drives routing, and routing decides whether a human has to
#: sit in a hold queue, so it is worth being explicit rather than asking a model to guess.
_CATEGORIES: dict[str, str] = {
    "anytime fitness": "gym",
    "planet fitness": "gym",
    "equinox": "gym",
    "24 hour fitness": "gym",
    "la fitness": "gym",
    "comcast": "cable",
    "xfinity": "cable",
    "spectrum": "cable",
    "directv": "satellite",
    "dish network": "satellite",
    "at&t": "telecom",
    "verizon": "telecom",
    "t-mobile": "telecom",
    "geico": "insurance",
    "progressive": "insurance",
    "state farm": "insurance",
    "allstate": "insurance",
    "adt": "alarm",
    "simplisafe": "alarm",
    "public storage": "storage",
    "extra space storage": "storage",
    "new york times": "newspaper",
    "wall street journal": "newspaper",
    "netflix": "streaming",
    "hulu": "streaming",
    "disney plus": "streaming",
    "max": "streaming",
    "spotify": "music",
    "apple music": "music",
    "tidal": "music",
    "dropbox": "cloud_storage",
    "google one": "cloud_storage",
    "icloud": "cloud_storage",
    "backblaze": "cloud_storage",
}


def normalize_vendor(descriptor: str) -> str:
    """Collapse a statement descriptor to a stable vendor name.

    Conservative on purpose. Over-merging two real vendors into one invents a cadence that
    does not exist, which is a worse failure than missing a leak.
    """
    text = " ".join(descriptor.split()).strip()
    if not text:
        return ""

    lowered = text.lower()
    for prefix in _PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip(" *-")
            lowered = text.lower()
            break

    # "purchase authorized on 03/14 anytime fitness" leaves a date behind.
    text = re.sub(r"^\d{1,2}/\d{1,2}(/\d{2,4})?\s+", "", text).strip()

    previous = None
    while previous != text:
        previous = text
        for pattern in _TRAILING:
            text = pattern.sub("", text).strip()

    text = text.strip(" *-#.,")
    if not text:
        return ""

    # Title case, but leave acronyms that were already fully upper and short.
    words = [w if (w.isupper() and len(w) <= 4) else w.capitalize() for w in text.split()]
    return " ".join(words)


def guess_category(vendor: str) -> str | None:
    """Map a vendor to a category, or None when we genuinely do not know."""
    key = vendor.lower().strip()
    if key in _CATEGORIES:
        return _CATEGORIES[key]
    for known, category in _CATEGORIES.items():
        if key.startswith(known) or known in key:
            return category
    return None


def _parse_date(raw: str) -> dt.date | None:
    text = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(raw: str) -> float | None:
    """Parse an amount, handling the several ways statements express a credit."""
    text = raw.strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace("$", "").replace(",", "").strip()
    if text.endswith("-"):           # trailing minus, seen in older exports
        negative, text = True, text[:-1]
    try:
        value = float(text)
    except ValueError:
        return None
    return -abs(value) if negative else value


def _pick(header: list[str], *candidates: str) -> int | None:
    lowered = [h.strip().lower() for h in header]
    for candidate in candidates:
        if candidate in lowered:
            return lowered.index(candidate)
    for index, name in enumerate(lowered):
        if any(candidate in name for candidate in candidates):
            return index
    return None


def parse_csv(text: str, *, source_prefix: str = "stmt") -> list[Charge]:
    """Parse a statement CSV into charges.

    Handles the common export shapes: a single signed amount column, or separate debit and
    credit columns. Rows that cannot be understood are skipped rather than guessed at,
    because a fabricated charge becomes a fabricated leak becomes a wrong phone call.
    """
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return []

    header = rows[0]
    date_i = _pick(header, "date", "transaction date", "posted date", "posting date")
    desc_i = _pick(header, "description", "merchant", "name", "payee", "details")
    amount_i = _pick(header, "amount")
    debit_i = _pick(header, "debit", "withdrawal")
    credit_i = _pick(header, "credit", "deposit")

    if date_i is None or desc_i is None:
        return []
    if amount_i is None and debit_i is None and credit_i is None:
        return []

    # Sign conventions differ between banks: some write purchases negative and refunds
    # positive, others do the reverse. Getting this backwards turns every refund into a
    # charge and every charge into a refund, so it is measured from the file rather than
    # assumed. On a real statement the overwhelming majority of rows are purchases.
    flip = False
    if amount_i is not None:
        values = [
            v for v in (_parse_amount(r[amount_i]) for r in rows[1:] if amount_i < len(r))
            if v
        ]
        if values:
            negatives = sum(1 for v in values if v < 0)
            flip = negatives > len(values) / 2

    charges: list[Charge] = []
    for line_no, row in enumerate(rows[1:], start=2):
        if max(filter(None, [date_i, desc_i, amount_i, debit_i, credit_i])) >= len(row):
            continue

        date = _parse_date(row[date_i])
        if date is None:
            continue

        vendor = normalize_vendor(row[desc_i])
        if not vendor:
            continue

        if amount_i is not None:
            amount = _parse_amount(row[amount_i])
            if amount is not None and flip:
                amount = -amount
        else:
            debit = _parse_amount(row[debit_i]) if debit_i is not None else None
            credit = _parse_amount(row[credit_i]) if credit_i is not None else None
            if debit:
                amount = abs(debit)
            elif credit:
                amount = -abs(credit)
            else:
                amount = None
        if amount is None or amount == 0:
            continue

        charges.append(
            Charge(
                vendor=vendor,
                amount_usd=round(amount, 2),
                date=date,
                source_id=f"{source_prefix}:{line_no}",
                category=guess_category(vendor),
            )
        )
    return charges


def parse_csv_file(path: str | Path) -> list[Charge]:
    p = Path(path)
    return parse_csv(p.read_text(encoding="utf-8", errors="replace"), source_prefix=p.stem)

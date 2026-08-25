"""Dated non-money facts about a vendor, pulled out of a mailbox.

Charges say what left the account. Signals say what was happening around it: a trial that
started, a return that was confirmed, a sign that the service was actually used. Without
signals the detector is limited to what a statement can honestly support, which is why
`detect` refuses to call anything a zombie when no engagement evidence exists.

The CSV shape here is the interchange format. A real mailbox connector produces the same
rows from message headers and bodies.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path

from dial.leaks import Signal, SignalKind

__all__ = ["parse_signals_csv", "parse_signals_csv_file"]

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")


def _parse_date(raw: str) -> dt.date | None:
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_signals_csv(text: str, *, source_prefix: str = "mail") -> list[Signal]:
    """Parse `vendor,kind,date` rows. Unknown kinds are skipped, never coerced."""
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        return []

    header = [h.strip().lower() for h in rows[0]]
    try:
        vendor_i = header.index("vendor")
        kind_i = header.index("kind")
        date_i = header.index("date")
    except ValueError:
        return []

    signals: list[Signal] = []
    for line_no, row in enumerate(rows[1:], start=2):
        if max(vendor_i, kind_i, date_i) >= len(row):
            continue
        date = _parse_date(row[date_i])
        vendor = row[vendor_i].strip()
        if date is None or not vendor:
            continue
        try:
            kind = SignalKind(row[kind_i].strip().lower())
        except ValueError:
            continue      # an unrecognised kind is dropped, not guessed at
        signals.append(
            Signal(vendor=vendor, kind=kind, date=date, source_id=f"{source_prefix}:{line_no}")
        )
    return signals


def parse_signals_csv_file(path: str | Path) -> list[Signal]:
    p = Path(path)
    return parse_signals_csv(p.read_text(encoding="utf-8", errors="replace"), source_prefix=p.stem)

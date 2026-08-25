"""The whole thing, end to end, from a statement file to a confirmation number.

    .venv/bin/python scripts/demo.py

Runs offline against the mock retention line. No AWS, no model, no phone. Swapping in Nova
Sonic and Amazon Connect replaces the brain and the line, not this flow.
"""

from __future__ import annotations

import datetime as dt
import textwrap
from pathlib import Path

from dial.brain import ScriptedBrain
from dial.caller import place_call
from dial.ingest.signals import parse_signals_csv_file
from dial.ingest.statements import parse_csv_file
from dial.leaks import Route, annual_cost, detect
from dial.mandate import Mandate, Objective

ROOT = Path(__file__).resolve().parents[1]
TODAY = dt.date(2026, 9, 1)
NOW = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc)

RULE = "-" * 86


def heading(text: str) -> None:
    print(f"\n{RULE}\n  {text}\n{RULE}")


def main() -> None:
    heading("1. READ THE STATEMENT")
    charges = parse_csv_file(ROOT / "fixtures" / "statement.csv")
    signals = parse_signals_csv_file(ROOT / "fixtures" / "signals.csv")
    vendors = sorted({c.vendor for c in charges})
    print(f"  {len(charges)} transactions, {len(vendors)} distinct vendors after normalization")
    print(f"  {', '.join(vendors)}")
    print(f"  {len(signals)} mailbox signals (trials, returns, signs of actual use)")

    heading("2. FIND WHAT IS LEAKING")
    leaks = detect(charges, signals, today=TODAY)
    for leak in leaks:
        amount = (f"${leak.annual_usd:>8,.2f}/yr" if leak.is_recurring
                  else f"${leak.one_time_usd:>8,.2f} once")
        print(f"\n  {amount}  {leak.vendor:<20} {leak.kind.value}")
        for line in textwrap.wrap(leak.rationale, 70):
            print(f"                 {line}")
        print(f"                 route: {leak.route.value}   "
              f"confidence: {leak.confidence:.0%}   evidence: {len(leak.evidence)} rows")

    print(f"\n  TOTAL BLEED: ${annual_cost(leaks):,.2f} a year")

    phone = [leak for leak in leaks if leak.route is Route.PHONE_REQUIRED]
    print(f"  Of that, ${annual_cost(phone):,.2f} can only be stopped by a phone call.")
    print("  That is not a coincidence. Retention lives behind a phone number.")

    heading("3. A HUMAN APPROVES ONE LEAK")
    target = max(phone, key=lambda leak: leak.annual_usd)
    print(f"  approving: {target.vendor}, ${target.annual_usd:,.2f} a year")
    mandate = Mandate(
        principal="Aria Han",
        vendor=target.vendor,
        objective=Objective.CANCEL,
        approved_at=NOW - dt.timedelta(minutes=1),
        expires_at=NOW + dt.timedelta(hours=1),
        acceptable=frozenset(),  # nothing. no discount, no pause, no downgrade.
    )
    print("  mandate: cancel only. No save is authorized.")
    print("  the agent cannot widen this while the call is running.")

    heading("4. MAKE THE CALL")
    from dial.mock_retention import RetentionLine

    line = RetentionLine(vendor=target.vendor, hold_turns=4)
    result = place_call(line, mandate, brain=ScriptedBrain(), now=NOW)

    for who, text in result.transcript:
        tag = "DIAL" if who == "dial" else "LINE"
        for i, chunk in enumerate(textwrap.wrap(text, 76)):
            print(f"  {tag if i == 0 else '':>4}  {chunk}")

    heading("5. THE RECEIPT")
    print(f"  outcome            : {result.outcome.name}")
    print(f"  confirmation number: {result.confirmation_number}")
    print(f"  call length        : {result.seconds_elapsed:.0f}s")
    print(f"  model consulted on hold music: {result.consulted_brain_on_hold}")
    print(f"  saves refused      : {sum(1 for _, ok, _ in result.rulings if not ok)}")
    for name, accepted, reason in result.rulings:
        print(f"     {'ACCEPTED' if accepted else 'REFUSED '}  {name}")
    print(f"\n  stopped: ${target.annual_usd:,.2f} a year\n")


if __name__ == "__main__":
    main()

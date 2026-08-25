"""Finding the money that leaves without being noticed.

Detection proposes. It never acts. Every leak carries the evidence it was derived from and
a confidence, and nothing reaches a dialer until a human approves that specific leak, because
the failure mode of a confident detector is cancelling something real.
"""

from __future__ import annotations

import datetime as dt
import enum
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

__all__ = [
    "Charge",
    "Signal",
    "SignalKind",
    "LeakKind",
    "Route",
    "Leak",
    "detect",
    "annual_cost",
]


class SignalKind(enum.Enum):
    """Non-money evidence pulled out of a mailbox."""

    TRIAL_STARTED = "trial_started"
    RETURN_CONFIRMED = "return_confirmed"
    ENGAGEMENT = "engagement"          # a login, a receipt of use, a shipping notice
    PRICE_CHANGE_NOTICE = "price_change_notice"
    RENEWAL_NOTICE = "renewal_notice"


class LeakKind(enum.Enum):
    CONVERTED_TRIAL = "converted_trial"
    SILENT_PRICE_RISE = "silent_price_rise"
    ZOMBIE_SERVICE = "zombie_service"
    DUPLICATE_SERVICE = "duplicate_service"
    DOUBLE_CHARGE = "double_charge"
    REFUND_NEVER_ISSUED = "refund_never_issued"
    AUTO_RENEWAL = "auto_renewal"


class Route(enum.Enum):
    """How this particular leak has to be stopped.

    The routing decision is the whole argument for the calling half of the product: for the
    vendors with the largest recurring charges, retention is deliberately gated behind a
    phone number.
    """

    SELF_SERVE = "self_serve"
    PHONE_REQUIRED = "phone_required"
    CERTIFIED_LETTER = "certified_letter"
    CARD_DISPUTE = "card_dispute"


@dataclass(frozen=True)
class Charge:
    """One money movement, from a receipt email or a statement row."""

    vendor: str
    amount_usd: float
    date: dt.date
    source_id: str
    category: str | None = None

    @property
    def is_credit(self) -> bool:
        return self.amount_usd < 0


@dataclass(frozen=True)
class Signal:
    """One dated non-money fact about a vendor."""

    vendor: str
    kind: SignalKind
    date: dt.date
    source_id: str


@dataclass(frozen=True)
class Leak:
    """A proposed leak. Evidence-carrying, not yet acted on."""

    kind: LeakKind
    vendor: str
    monthly_usd: float
    confidence: float
    rationale: str
    evidence: tuple[str, ...]
    route: Route = Route.PHONE_REQUIRED
    approved: bool = False

    @property
    def annual_usd(self) -> float:
        return round(self.monthly_usd * 12, 2)


#: Vendor categories where cancellation is, in practice, gated behind a human on a phone.
#: Sourced from how these industries actually operate rather than from what their websites
#: claim, which is why it is a constant here and not a guess the model makes at runtime.
PHONE_GATED_CATEGORIES = frozenset(
    {"gym", "insurance", "telecom", "cable", "satellite", "alarm", "storage", "newspaper"}
)

_MONTHLY = 30.0

#: Real billing periods. A vendor bills weekly, fortnightly, monthly, quarterly, or yearly.
#: Nothing legitimate bills every 77 days, so a "cadence" that matches none of these is
#: coincidence in noisy data rather than a subscription.
BILLING_PERIODS = (7.0, 14.0, 30.0, 31.0, 90.0, 365.0)


def _snap_to_billing_period(median_gap: float) -> float | None:
    """Snap an observed gap to a real billing period, or reject it.

    Tolerance is proportional, because six days of drift is normal on a yearly renewal and
    absurd on a weekly one.
    """
    for period in BILLING_PERIODS:
        if abs(median_gap - period) <= max(3.0, period * 0.08):
            return period
    return None


def _cadence_days(dates: list[dt.date]) -> float | None:
    """The billing period these charges follow, or None if they do not follow one."""
    if len(dates) < 3:
        return None
    ordered = sorted(dates)
    gaps = [(b - a).days for a, b in zip(ordered, ordered[1:])]
    if not gaps:
        return None
    median = statistics.median(gaps)
    if median <= 0:
        return None
    period = _snap_to_billing_period(median)
    if period is None:
        return None
    if max(abs(g - median) for g in gaps) > max(3.0, period * 0.25):
        return None
    return period


def _amounts_are_subscription_shaped(ordered: list[Charge]) -> bool:
    """A subscription charges the same amount, or steps once to a new amount and holds.

    Three different amounts from the same vendor is a shop, not a subscription. This is the
    check that stops the detector reporting someone's corner store as a recurring bill.
    """
    amounts = [round(c.amount_usd, 2) for c in ordered]
    distinct = list(dict.fromkeys(amounts))
    if len(distinct) == 1:
        return True
    if len(distinct) != 2:
        return False
    # Two levels are only credible as a price change: everything at the old price comes
    # before everything at the new one.
    first, second = distinct
    switch = amounts.index(second)
    return all(a == first for a in amounts[:switch]) and all(
        a == second for a in amounts[switch:]
    )


def _to_monthly(amount: float, cadence_days: float) -> float:
    return round(amount * (_MONTHLY / cadence_days), 2)


def _route_for(vendor_category: str | None, kind: LeakKind) -> Route:
    if kind is LeakKind.DOUBLE_CHARGE:
        return Route.CARD_DISPUTE
    if kind is LeakKind.REFUND_NEVER_ISSUED:
        return Route.CARD_DISPUTE
    if vendor_category in PHONE_GATED_CATEGORIES:
        return Route.PHONE_REQUIRED
    return Route.SELF_SERVE


def detect(
    charges: list[Charge],
    signals: list[Signal] | None = None,
    *,
    today: dt.date | None = None,
) -> list[Leak]:
    """Propose leaks from charges and mailbox signals, ranked by annual cost."""
    signals = signals or []
    today = today or dt.date.today()
    found: list[Leak] = []

    debits = [c for c in charges if not c.is_credit]
    credits = [c for c in charges if c.is_credit]

    by_vendor: dict[str, list[Charge]] = defaultdict(list)
    for c in debits:
        by_vendor[c.vendor].append(c)

    sig_by_vendor: dict[str, list[Signal]] = defaultdict(list)
    for s in signals:
        sig_by_vendor[s.vendor].append(s)

    found += _double_charges(by_vendor)
    found += _missing_refunds(credits, sig_by_vendor, today)

    for vendor, vendor_charges in by_vendor.items():
        cadence = _cadence_days([c.date for c in vendor_charges])
        if cadence is None:
            continue
        ordered = sorted(vendor_charges, key=lambda c: c.date)
        if not _amounts_are_subscription_shaped(ordered):
            continue
        category = next((c.category for c in ordered if c.category), None)

        rise = _price_rise(vendor, ordered, cadence, category)
        if rise:
            found.append(rise)

        trial = _converted_trial(vendor, ordered, cadence, sig_by_vendor[vendor], category)
        if trial:
            found.append(trial)

        zombie = _zombie(vendor, ordered, cadence, sig_by_vendor[vendor], category, today)
        if zombie:
            found.append(zombie)

    found += _duplicates(by_vendor)

    # Highest annual cost first: that is the order a human wants to approve in.
    return sorted(found, key=lambda leak: leak.annual_usd, reverse=True)


def _price_rise(
    vendor: str, ordered: list[Charge], cadence: float, category: str | None
) -> Leak | None:
    amounts = [c.amount_usd for c in ordered]
    baseline, latest = amounts[0], amounts[-1]
    if latest <= baseline * 1.05:
        return None
    delta = latest - baseline
    pct = (delta / baseline) * 100
    return Leak(
        kind=LeakKind.SILENT_PRICE_RISE,
        vendor=vendor,
        monthly_usd=_to_monthly(delta, cadence),
        confidence=0.9 if pct >= 20 else 0.75,
        rationale=(
            f"{vendor} went from ${baseline:.2f} to ${latest:.2f}, "
            f"up {pct:.0f} percent, on the same {int(cadence)} day cadence"
        ),
        evidence=(ordered[0].source_id, ordered[-1].source_id),
        route=_route_for(category, LeakKind.SILENT_PRICE_RISE),
    )


def _converted_trial(
    vendor: str,
    ordered: list[Charge],
    cadence: float,
    vendor_signals: list[Signal],
    category: str | None,
) -> Leak | None:
    trials = [s for s in vendor_signals if s.kind is SignalKind.TRIAL_STARTED]
    if not trials:
        return None
    trial = min(trials, key=lambda s: s.date)
    first_charge = ordered[0]
    gap = (first_charge.date - trial.date).days
    if not 7 <= gap <= 45:
        return None
    engaged = any(s.kind is SignalKind.ENGAGEMENT for s in vendor_signals)
    return Leak(
        kind=LeakKind.CONVERTED_TRIAL,
        vendor=vendor,
        monthly_usd=_to_monthly(first_charge.amount_usd, cadence),
        confidence=0.85 if not engaged else 0.6,
        rationale=(
            f"free trial started {trial.date.isoformat()}, first charge {gap} days later, "
            + ("no sign of use since" if not engaged else "some use since")
        ),
        evidence=(trial.source_id, first_charge.source_id),
        route=_route_for(category, LeakKind.CONVERTED_TRIAL),
    )


def _zombie(
    vendor: str,
    ordered: list[Charge],
    cadence: float,
    vendor_signals: list[Signal],
    category: str | None,
    today: dt.date,
) -> Leak | None:
    last_use = max(
        (s.date for s in vendor_signals if s.kind is SignalKind.ENGAGEMENT),
        default=None,
    )
    quiet_days = (today - last_use).days if last_use else (today - ordered[0].date).days
    if quiet_days < 120:
        return None
    latest = ordered[-1]
    return Leak(
        kind=LeakKind.ZOMBIE_SERVICE,
        vendor=vendor,
        monthly_usd=_to_monthly(latest.amount_usd, cadence),
        confidence=0.8 if last_use else 0.65,
        rationale=(
            f"charging every {int(cadence)} days, "
            f"no sign of use in {quiet_days} days"
        ),
        evidence=tuple(c.source_id for c in ordered[-3:]),
        route=_route_for(category, LeakKind.ZOMBIE_SERVICE),
    )


def _double_charges(by_vendor: dict[str, list[Charge]]) -> list[Leak]:
    out: list[Leak] = []
    for vendor, vendor_charges in by_vendor.items():
        seen: dict[tuple[dt.date, float], Charge] = {}
        for c in sorted(vendor_charges, key=lambda x: x.date):
            key = (c.date, round(c.amount_usd, 2))
            if key in seen:
                out.append(
                    Leak(
                        kind=LeakKind.DOUBLE_CHARGE,
                        vendor=vendor,
                        monthly_usd=round(c.amount_usd, 2),
                        confidence=0.95,
                        rationale=(
                            f"two charges of ${c.amount_usd:.2f} from {vendor} "
                            f"on {c.date.isoformat()}"
                        ),
                        evidence=(seen[key].source_id, c.source_id),
                        route=Route.CARD_DISPUTE,
                    )
                )
            else:
                seen[key] = c
    return out


def _missing_refunds(
    credits: list[Charge],
    sig_by_vendor: dict[str, list[Signal]],
    today: dt.date,
) -> list[Leak]:
    out: list[Leak] = []
    for vendor, vendor_signals in sig_by_vendor.items():
        for s in vendor_signals:
            if s.kind is not SignalKind.RETURN_CONFIRMED:
                continue
            if (today - s.date).days < 21:
                continue  # still inside a normal refund window
            refunded = any(
                c.vendor == vendor and c.date >= s.date for c in credits
            )
            if refunded:
                continue
            out.append(
                Leak(
                    kind=LeakKind.REFUND_NEVER_ISSUED,
                    vendor=vendor,
                    monthly_usd=0.0,
                    confidence=0.7,
                    rationale=(
                        f"return to {vendor} confirmed {s.date.isoformat()}, "
                        f"no matching credit in {(today - s.date).days} days"
                    ),
                    evidence=(s.source_id,),
                    route=Route.CARD_DISPUTE,
                )
            )
    return out


def _duplicates(by_vendor: dict[str, list[Charge]]) -> list[Leak]:
    by_category: dict[str, set[str]] = defaultdict(set)
    latest: dict[str, Charge] = {}
    for vendor, vendor_charges in by_vendor.items():
        ordered = sorted(vendor_charges, key=lambda c: c.date)
        category = next((c.category for c in ordered if c.category), None)
        if category:
            by_category[category].add(vendor)
        latest[vendor] = ordered[-1]

    out: list[Leak] = []
    for category, vendors in by_category.items():
        if len(vendors) < 2:
            continue
        # Propose dropping the cheaper-to-lose one: the smallest recurring charge in the
        # category. Which one actually goes is the human's call, not ours.
        cheapest = min(vendors, key=lambda v: latest[v].amount_usd)
        charge = latest[cheapest]
        others = sorted(vendors - {cheapest})
        out.append(
            Leak(
                kind=LeakKind.DUPLICATE_SERVICE,
                vendor=cheapest,
                monthly_usd=round(charge.amount_usd, 2),
                confidence=0.55,
                rationale=(
                    f"paying for {len(vendors)} {category} services: "
                    f"{cheapest} alongside {', '.join(others)}"
                ),
                evidence=(charge.source_id,),
                route=_route_for(category, LeakKind.DUPLICATE_SERVICE),
            )
        )
    return out


def annual_cost(leaks: list[Leak], *, approved_only: bool = False) -> float:
    """Total annual bleed across leaks. The number that goes in the pitch."""
    considered = [leak for leak in leaks if leak.approved] if approved_only else leaks
    return round(sum(leak.annual_usd for leak in considered), 2)

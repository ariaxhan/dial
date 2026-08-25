"""Turning a mailbox into charges and signals.

A statement says money left. A mailbox says why, and it is the only place that knows a trial
started, a return was accepted, or that you actually opened the thing you are paying for. The
detector refuses to call a subscription unused without that last one, so this module is what
makes the strongest finding possible at all.

The hard part is not reading email, it is not being fooled by it. Marketing mail is designed
to look like a receipt: it contains a vendor, a dollar amount, and an urgent date. Extracting
`$50` from "save $50 when you upgrade" and calling it a charge would invent a subscription
that does not exist and eventually place a phone call about it.

So the rule here is the same as everywhere else in this codebase: when the evidence is
ambiguous, produce nothing.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from dial.ingest.statements import guess_category, normalize_vendor
from dial.leaks import Charge, Signal, SignalKind

__all__ = ["Message", "vendor_from_sender", "extract", "extract_charges", "extract_signals"]


@dataclass(frozen=True)
class Message:
    """One email, reduced to what matters. Produced by any connector."""

    id: str
    sender: str
    subject: str
    date: dt.date
    body: str = ""

    @property
    def text(self) -> str:
        return f"{self.subject}\n{self.body}"


#: Sender domains that never identify a vendor, so the subject has to be used instead.
_GENERIC_DOMAINS = frozenset(
    {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
     "me.com", "proton.me", "protonmail.com", "aol.com"}
)

#: Mail infrastructure labels that sit in front of the real domain.
_MAIL_SUBDOMAINS = frozenset(
    {"mail", "email", "e", "em", "news", "info", "no-reply", "noreply", "reply", "notify",
     "notifications", "message", "messages", "send", "sender", "mailer", "smtp", "t", "u",
     "click", "links", "link", "go", "cs", "support", "billing", "receipts", "order"}
)

_ADDRESS = re.compile(r"[\w.+-]+@([\w.-]+)")

# Money, with the context that makes it a charge rather than an advertisement. The keyword
# must sit near the amount, which is what separates "Total: $49.00" from "save $49".
_CHARGE_CONTEXT = re.compile(
    r"(?:^|\n|\s)"
    r"(?:order\s+total|grand\s+total|total\s+charged|total\s+due|amount\s+charged|"
    r"amount\s+due|amount\s+paid|you\s+(?:were\s+)?charged|we\s+charged|charged\s+to|"
    r"payment\s+of|paid|total|subtotal\s+total)"
    r"\s*[:\-]?\s*"
    r"(?:USD\s*)?\$\s*([0-9][0-9,]*\.[0-9]{2})",
    re.I,
)

#: Phrases that mean this mail is an advertisement, whatever else it contains.
_MARKETING = re.compile(
    r"\b(save \$|% off|limited time|upgrade (now|today)|special offer|deal of the|"
    r"don'?t miss|last chance|shop now|unsubscribe from marketing)\b",
    re.I,
)

#: A receipt says so somewhere. Without one of these, an amount is not treated as a charge.
_RECEIPT_MARKER = re.compile(
    r"\b(receipt|invoice|your (payment|order|subscription)|payment (received|confirmation|"
    r"successful)|thanks? for your (payment|order)|billing statement|has been charged|"
    r"we(?:'ve| have) charged|renewed|renewal confirmation)\b",
    re.I,
)

_REFUND_MARKER = re.compile(
    r"\b(refunded|refund (has been |was )?(issued|processed|completed)|"
    r"credited back|credited to your|money back)",
    re.I,
)

_TRIAL = re.compile(
    r"\b(free trial|trial (has )?(begun|started|starts)|your trial|"
    r"welcome to your (\d+ ?day )?trial|trial period)\b",
    re.I,
)

_RETURN_CONFIRMED = re.compile(
    r"\b(we(?:'ve| have) received your return|your return (is|has been) (complete|received|"
    r"accepted|processed)|return confirmed|item returned)\b",
    re.I,
)

_PRICE_CHANGE = re.compile(
    r"\b(price (change|increase|update)|an update to your (monthly )?(rate|price|plan)|"
    r"changes to your (subscription|plan) price|your rate is changing)\b",
    re.I,
)

_RENEWAL_NOTICE = re.compile(
    r"\b(will (automatically )?renew|auto[- ]renew|renews on|upcoming renewal)", re.I
)

#: Signs the service was actually used. Deliberately narrow: a false engagement signal is
#: worse than a missing one, because it suppresses a real finding.
_ENGAGEMENT = re.compile(
    r"\b(check[- ]?in confirm|you checked in|new sign[- ]?in|signed in|login from|"
    r"continue watching|you watched|because you watched|shared with you|"
    r"your (weekly|monthly) (summary|recap|report)|workout|your download is ready|"
    r"has shipped|out for delivery|you listened)",
    re.I,
)


def vendor_from_sender(sender: str, *, subject: str = "") -> str:
    """Work out the vendor from an address, falling back to the subject.

    The sending domain is far more reliable than the display name, which vendors change
    seasonally and marketers stuff with adjectives.
    """
    match = _ADDRESS.search(sender or "")
    if not match:
        return normalize_vendor(subject) if subject else ""

    domain = match.group(1).lower().rstrip(".")

    # A personal mailbox tells us nothing about the vendor, and stripping it down would
    # happily produce a company called "Gmail". Fall back to the subject instead.
    if domain in _GENERIC_DOMAINS:
        return normalize_vendor(subject) if subject else ""

    labels = [label for label in domain.split(".") if label]
    if not labels:
        return ""

    # Drop the public suffix, roughly. Two-label suffixes like co.uk lose one more.
    if len(labels) >= 3 and labels[-2] in {"co", "com", "org", "net", "ac", "gov"}:
        labels = labels[:-2]
    elif len(labels) >= 2:
        labels = labels[:-1]

    while len(labels) > 1 and labels[0] in _MAIL_SUBDOMAINS:
        labels = labels[1:]

    registrable = ".".join(labels)
    if not registrable or f"{registrable}.com" in _GENERIC_DOMAINS or registrable in {
        d.split(".")[0] for d in _GENERIC_DOMAINS
    }:
        return normalize_vendor(subject) if subject else ""

    # "anytimefitness" needs to reach "Anytime Fitness", which the known-vendor table can
    # do once the run-together form is spaced out enough to match.
    candidate = labels[-1] if len(labels) > 1 else registrable
    return normalize_vendor(_respace(candidate))


def _respace(token: str) -> str:
    """Split a run-together domain label against known vendor names.

    'anytimefitness' is one label but two words, and the category table is keyed on the
    spaced form.
    """
    from dial.ingest.statements import _CATEGORIES

    flat = token.replace("-", "").replace("_", "").lower()
    for known in _CATEGORIES:
        if known.replace(" ", "") == flat:
            return known
    return token.replace("-", " ").replace("_", " ")


def _amount(text: str) -> float | None:
    """The charged amount, or None when nothing in the mail says one was charged."""
    if _MARKETING.search(text) and not _RECEIPT_MARKER.search(text):
        return None
    match = _CHARGE_CONTEXT.search(text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def extract_charges(messages: list[Message]) -> list[Charge]:
    """Charges from receipt mail. Anything ambiguous produces nothing."""
    charges: list[Charge] = []
    for message in messages:
        text = message.text
        if not _RECEIPT_MARKER.search(text) and not _REFUND_MARKER.search(text):
            continue

        amount = _amount(text)
        if amount is None or amount <= 0:
            continue

        vendor = vendor_from_sender(message.sender, subject=message.subject)
        if not vendor:
            continue

        # A refund is the same shape as a receipt with the sign reversed, and it almost
        # always mentions the original order, so refund language wins outright rather than
        # being disqualified by the receipt words sitting next to it.
        is_refund = bool(_REFUND_MARKER.search(text))
        charges.append(
            Charge(
                vendor=vendor,
                amount_usd=-amount if is_refund else amount,
                date=message.date,
                source_id=f"mail:{message.id}",
                category=guess_category(vendor),
            )
        )
    return charges


def extract_signals(messages: list[Message]) -> list[Signal]:
    """Dated facts about a vendor. A message can carry more than one."""
    signals: list[Signal] = []
    for message in messages:
        text = message.text
        vendor = vendor_from_sender(message.sender, subject=message.subject)
        if not vendor:
            continue

        found: list[SignalKind] = []
        if _TRIAL.search(text):
            found.append(SignalKind.TRIAL_STARTED)
        if _RETURN_CONFIRMED.search(text):
            found.append(SignalKind.RETURN_CONFIRMED)
        if _PRICE_CHANGE.search(text):
            found.append(SignalKind.PRICE_CHANGE_NOTICE)
        if _RENEWAL_NOTICE.search(text):
            found.append(SignalKind.RENEWAL_NOTICE)
        # Engagement only counts when the mail is not itself a receipt: being billed is
        # not evidence that you used the thing, which is the entire point of the finding.
        if _ENGAGEMENT.search(text) and not _RECEIPT_MARKER.search(text):
            found.append(SignalKind.ENGAGEMENT)

        for kind in found:
            signals.append(
                Signal(
                    vendor=vendor,
                    kind=kind,
                    date=message.date,
                    source_id=f"mail:{message.id}",
                )
            )
    return signals


def extract(messages: list[Message]) -> tuple[list[Charge], list[Signal]]:
    """Everything usable in one pass."""
    return extract_charges(messages), extract_signals(messages)

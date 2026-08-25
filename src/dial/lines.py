"""Which numbers Dial is allowed to call.

This module is the legal boundary of the product, expressed in code rather than in a
prompt, because a prompt is a suggestion and this is not.

The FCC treats an AI-generated voice as an "artificial or prerecorded voice" under the
TCPA. That classification restricts two things:

    47 USC 227(b)(1)(B)      artificial voice to a *residential* telephone line
    47 USC 227(b)(1)(A)(iii) artificial voice to a *wireless* number the callee pays for

Neither subsection reaches a company's published business line. So Dial calls published
business lines, and refuses everything else before a dialer is ever constructed. A refusal
here is not a warning the caller agent can talk itself out of; it is the absence of a
number to dial.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

import phonenumbers
from phonenumbers import PhoneNumberType, carrier, number_type

__all__ = ["LineKind", "LineVerdict", "classify", "assert_callable", "RefusedNumber"]


class LineKind(enum.Enum):
    """What kind of line a number resolves to, in TCPA-relevant terms."""

    TOLL_FREE = "toll_free"
    BUSINESS_LANDLINE = "business_landline"
    RESIDENTIAL_OR_UNKNOWN_LANDLINE = "residential_or_unknown_landline"
    WIRELESS = "wireless"
    VOIP = "voip"
    UNPARSEABLE = "unparseable"


#: Kinds Dial will place an AI-voice call to. Deliberately short.
CALLABLE_KINDS = frozenset({LineKind.TOLL_FREE, LineKind.BUSINESS_LANDLINE})

#: North American toll-free area codes. These are business lines by definition; no
#: consumer is billed for the call and no residence is assigned one.
TOLL_FREE_NPAS = frozenset({"800", "833", "844", "855", "866", "877", "888"})

_DIGITS = re.compile(r"\D+")


class RefusedNumber(RuntimeError):
    """Raised when something asks Dial to call a number it will not call."""

    def __init__(self, verdict: "LineVerdict") -> None:
        self.verdict = verdict
        super().__init__(verdict.reason)


@dataclass(frozen=True)
class LineVerdict:
    """The result of classifying a number, and why."""

    raw: str
    e164: str | None
    kind: LineKind
    callable: bool
    reason: str

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.callable


def _npa(parsed: phonenumbers.PhoneNumber) -> str | None:
    """North American area code, or None outside the NANP."""
    if parsed.country_code != 1:
        return None
    national = _DIGITS.sub("", str(parsed.national_number))
    return national[:3] if len(national) == 10 else None


def classify(raw: str, *, region: str = "US") -> LineVerdict:
    """Classify a phone number without dialing it.

    Unknown is treated as unsafe. A landline that cannot be shown to be a business line
    is refused, because the cost of being wrong is a statutory violation per call and the
    cost of being conservative is one unplaced call.
    """
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return LineVerdict(raw, None, LineKind.UNPARSEABLE, False, "not a phone number")

    if not phonenumbers.is_valid_number(parsed):
        return LineVerdict(raw, None, LineKind.UNPARSEABLE, False, "not a valid number")

    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

    if _npa(parsed) in TOLL_FREE_NPAS:
        return LineVerdict(
            raw, e164, LineKind.TOLL_FREE, True,
            "toll free, a business line by definition",
        )

    kind = number_type(parsed)

    if kind in (PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE):
        return LineVerdict(
            raw, e164, LineKind.WIRELESS, False,
            "wireless or possibly wireless, restricted by 227(b)(1)(A)(iii)",
        )

    if kind == PhoneNumberType.VOIP:
        return LineVerdict(
            raw, e164, LineKind.VOIP, False,
            "VoIP, cannot be shown to be a business line",
        )

    if kind == PhoneNumberType.FIXED_LINE:
        return LineVerdict(
            raw, e164, LineKind.RESIDENTIAL_OR_UNKNOWN_LANDLINE, False,
            "landline that is not demonstrably a business line, "
            "restricted by 227(b)(1)(B) if residential",
        )

    return LineVerdict(
        raw, e164, LineKind.RESIDENTIAL_OR_UNKNOWN_LANDLINE, False,
        f"unclassifiable line type ({kind}), refused as unknown",
    )


def assert_callable(raw: str, *, region: str = "US") -> str:
    """Return the E.164 number, or raise. The only sanctioned way to get a dial string.

    Every dialer in this codebase takes its number from here. There is no other path to a
    number, which is what makes the boundary real rather than advisory.
    """
    verdict = classify(raw, region=region)
    if not verdict.callable:
        raise RefusedNumber(verdict)
    assert verdict.e164 is not None
    return verdict.e164

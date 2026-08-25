"""What the agent is allowed to agree to, decided before the call and never during it.

A voice agent on a live call is talking to a trained retention specialist whose entire job
is to move the boundary. "I can pause it for three months instead" is not an offer, it is a
probe. If the agent's authority lives in its system prompt, that probe eventually works,
because a prompt is text and the retention script is also text and the model has no way to
tell which one is the constitution.

So authority lives here instead: a frozen object, created before the dialer exists,
approved by a human for one specific leak, expiring on a clock. The caller agent reads it
and cannot write it. Anything said on the call is *information*, never authority.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass, field, replace

__all__ = [
    "Objective",
    "Concession",
    "Mandate",
    "MandateViolation",
    "evaluate_offer",
    "OfferRuling",
]


class Objective(enum.Enum):
    """The single thing this call is for."""

    CANCEL = "cancel"
    REFUND = "refund"
    DOWNGRADE = "downgrade"
    DISPUTE = "dispute"
    RESTORE_PRIOR_PRICE = "restore_prior_price"


class Concession(enum.Enum):
    """Things a vendor will offer instead of doing what was asked.

    Named explicitly so that accepting one is a decision the human made in advance,
    rather than a decision the model made at minute eleven of a difficult call.
    """

    DISCOUNT_OFFER = "discount_offer"
    PAUSE_INSTEAD_OF_CANCEL = "pause_instead_of_cancel"
    DOWNGRADE_INSTEAD_OF_CANCEL = "downgrade_instead_of_cancel"
    PARTIAL_REFUND = "partial_refund"
    CREDIT_INSTEAD_OF_REFUND = "credit_instead_of_refund"
    NEW_CONTRACT_TERM = "new_contract_term"
    CALLBACK_LATER = "callback_later"


#: Never acceptable, on any call, regardless of what the mandate says. A human cannot
#: opt into these through the approval flow because they are not offers, they are traps.
ALWAYS_REFUSED = frozenset({Concession.NEW_CONTRACT_TERM})

#: Facts the agent will not say out loud on a phone call even if asked directly, and even
#: if the human supplied them. Verifying identity is the vendor's problem to solve another
#: way; an AI voice reciting a social security number to whoever answered is not a risk
#: worth any amount of convenience.
NEVER_DISCLOSE = frozenset(
    {
        "ssn",
        "social_security_number",
        "full_card_number",
        "cvv",
        "password",
        "one_time_passcode",
        "security_answer",
    }
)


class MandateViolation(RuntimeError):
    """Raised when something tries to act outside, or widen, a mandate."""


@dataclass(frozen=True)
class Mandate:
    """Authority for exactly one objective against exactly one vendor.

    Frozen on purpose. `widen` does not exist. To get more authority you go back to the
    human, which is the entire point.
    """

    principal: str
    vendor: str
    objective: Objective
    approved_at: dt.datetime
    expires_at: dt.datetime
    acceptable: frozenset[Concession] = field(default_factory=frozenset)
    #: Ceiling in dollars on anything the agent agrees to *pay*. Cancelling costs nothing,
    #: so this is normally zero, and a nonzero value is a deliberate human choice.
    max_spend_usd: float = 0.0
    #: Floor in dollars on a refund the agent will accept as settling the matter.
    min_acceptable_refund_usd: float = 0.0
    disclose_ai: bool = True

    def __post_init__(self) -> None:
        if self.expires_at <= self.approved_at:
            raise MandateViolation("mandate expires before it begins")
        if self.max_spend_usd < 0 or self.min_acceptable_refund_usd < 0:
            raise MandateViolation("negative money in a mandate")
        if not self.disclose_ai:
            raise MandateViolation(
                "AI disclosure is not optional and cannot be waived by a mandate"
            )
        forbidden = self.acceptable & ALWAYS_REFUSED
        if forbidden:
            raise MandateViolation(
                f"these are never acceptable: {sorted(c.value for c in forbidden)}"
            )

    def live_at(self, now: dt.datetime) -> bool:
        return self.approved_at <= now < self.expires_at

    def assert_live(self, now: dt.datetime) -> None:
        if not self.live_at(now):
            raise MandateViolation(
                f"mandate for {self.vendor} is not live at {now.isoformat()}"
            )

    def narrowed(self, **changes: object) -> "Mandate":
        """Produce a stricter mandate. Attempts to loosen one are refused.

        Narrowing is safe and occasionally useful (a supervisor lane tightening a lane
        before handing it down). Widening is the attack, so it is checked rather than
        trusted.
        """
        candidate = replace(self, **changes)  # type: ignore[arg-type]
        if not candidate.acceptable <= self.acceptable:
            raise MandateViolation("narrowed() cannot add acceptable concessions")
        if candidate.max_spend_usd > self.max_spend_usd:
            raise MandateViolation("narrowed() cannot raise the spend ceiling")
        if candidate.expires_at > self.expires_at:
            raise MandateViolation("narrowed() cannot extend the expiry")
        if candidate.vendor != self.vendor or candidate.objective is not self.objective:
            raise MandateViolation("narrowed() cannot retarget a mandate")
        return candidate


@dataclass(frozen=True)
class OfferRuling:
    """Whether the agent may accept what it was just offered, and what to say."""

    accept: bool
    reason: str
    #: True when the call cannot succeed and should be escalated rather than continued.
    escalate: bool = False


def evaluate_offer(
    mandate: Mandate,
    concession: Concession,
    *,
    now: dt.datetime,
    amount_usd: float = 0.0,
    costs_usd: float = 0.0,
) -> OfferRuling:
    """Rule on a vendor's counter-offer against the mandate.

    Called by the caller agent as a tool. The model decides *what it heard*; this function
    decides *what that means*. Keeping those two jobs apart is what stops a persuasive
    retention script from becoming an authorization.
    """
    mandate.assert_live(now)

    if concession in ALWAYS_REFUSED:
        return OfferRuling(False, f"{concession.value} is never acceptable", escalate=False)

    if concession not in mandate.acceptable:
        return OfferRuling(
            False,
            f"{concession.value} was not approved for this call",
        )

    if costs_usd > mandate.max_spend_usd:
        return OfferRuling(
            False,
            f"costs ${costs_usd:.2f}, ceiling is ${mandate.max_spend_usd:.2f}",
        )

    refund_like = {Concession.PARTIAL_REFUND, Concession.CREDIT_INSTEAD_OF_REFUND}
    if concession in refund_like and amount_usd < mandate.min_acceptable_refund_usd:
        return OfferRuling(
            False,
            f"${amount_usd:.2f} is below the ${mandate.min_acceptable_refund_usd:.2f} floor",
        )

    if concession is Concession.CALLBACK_LATER:
        return OfferRuling(True, "callback accepted, call did not resolve", escalate=True)

    return OfferRuling(True, f"{concession.value} is within mandate")

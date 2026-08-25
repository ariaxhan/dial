"""Understanding what the other end just said.

The brain has exactly one job: turn an utterance into a `Perception`. It decides *what it
heard*. It never decides *what that means for the mandate*, and it never decides what to do
next. Those live in `dial.caller` and `dial.mandate` respectively.

Keeping the split this sharp is what stops a persuasive retention script from becoming an
authorization. A model that both interprets the sentence and rules on it can be argued with.
A model that only interprets cannot, because the ruling happens somewhere it cannot reach.

Two implementations. `ScriptedBrain` is rule-based, runs in tests with no model and no
network, and is what proves the loop's shape. `SonicBrain` will wrap a Strands agent on Nova
Sonic and is a drop-in for the same protocol.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from dial.mandate import Concession

__all__ = ["Perception", "Brain", "ScriptedBrain"]


@dataclass(frozen=True)
class Perception:
    """What the caller agent understood from one thing the line said."""

    #: Hold music or an automated queue message. While true, the model is not consulted.
    is_hold: bool = False
    #: A live human is on the line.
    is_human: bool = False
    #: The IVR wants a key pressed, and this is which one.
    dtmf: str | None = None
    #: A counter-offer, mapped to the vocabulary the mandate rules on.
    offer: Concession | None = None
    #: Pressure with nothing to accept. Not an offer, so there is no ruling to make.
    is_pressure: bool = False
    #: The line is asking for something, by key. Checked against NEVER_DISCLOSE.
    asks_for: str | None = None
    #: The vendor will not do this by phone at all.
    refuses_by_phone: bool = False
    #: The objective was completed, with this confirmation.
    confirmation_number: str | None = None
    #: Reached an answering machine.
    is_voicemail: bool = False


class Brain(Protocol):
    """Anything that can hear. Swapped for Nova Sonic without touching the call loop."""

    def perceive(self, utterance: str) -> Perception: ...


_HOLD = re.compile(r"\b(hold music|please hold|your call is important|still holding)\b", re.I)
_HUMAN = re.compile(r"\b(this is \w+|how can i help|what can i do for you|thanks for holding)\b", re.I)
_VOICEMAIL = re.compile(r"\b(leave a message|after the tone|voicemail|not available right now)\b", re.I)
_DTMF = re.compile(r"\bfor billing[, ]+press (\d)\b", re.I)
_DTMF_FALLBACK = re.compile(r"\bpress (\d)\b", re.I)
_CONFIRMATION = re.compile(r"\bconfirmation number is ([A-Z]{2}-\d+)\b", re.I)
_REFUSES_BY_PHONE = re.compile(
    r"\b(in writing|can'?t process that over the phone|by mail|corporate office)\b", re.I
)
_ASKS_SSN = re.compile(r"\b(social security|social)\b", re.I)
_ASKS_CARD = re.compile(r"\b(full card number|entire card number|sixteen digit)\b", re.I)
_ASKS_VERIFY = re.compile(r"\b(confirm the name|last four|verify)\b", re.I)

#: Utterance patterns to the concession vocabulary the mandate speaks.
_OFFERS: tuple[tuple[re.Pattern[str], Concession], ...] = (
    (re.compile(r"\b(percent off|discount|reduced rate|lower your (rate|bill))\b", re.I),
     Concession.DISCOUNT_OFFER),
    (re.compile(r"\b(freeze|pause|suspend|put it on hold for)\b", re.I),
     Concession.PAUSE_INSTEAD_OF_CANCEL),
    (re.compile(r"\b(basic tier|downgrade|cheaper plan|lower plan)\b", re.I),
     Concession.DOWNGRADE_INSTEAD_OF_CANCEL),
    (re.compile(r"\b(partial refund|refund part|credit back)\b", re.I),
     Concession.PARTIAL_REFUND),
    (re.compile(r"\b(account credit|store credit|credit on your account)\b", re.I),
     Concession.CREDIT_INSTEAD_OF_REFUND),
    (re.compile(r"\b(call you back|have her call|take a number)\b", re.I),
     Concession.CALLBACK_LATER),
    (re.compile(r"\b(new (twelve|24|twenty four) month|renew your (contract|term)|"
                r"sign a new (agreement|contract))\b", re.I),
     Concession.NEW_CONTRACT_TERM),
)

_PRESSURE = re.compile(
    r"\b(permanently|won'?t be able to get it back|are you sure|you'?ll lose|"
    r"most people prefer)\b",
    re.I,
)


class ScriptedBrain:
    """Rule-based perception. Deterministic, offline, and good enough to prove the loop.

    The production brain is a model. This one exists so the call loop, the hold gate, and the
    mandate can all be tested without AWS, without a network, and without a phone.
    """

    def perceive(self, utterance: str) -> Perception:
        text = utterance or ""

        if _HOLD.search(text):
            return Perception(is_hold=True)

        if _VOICEMAIL.search(text):
            return Perception(is_voicemail=True)

        confirmation = _CONFIRMATION.search(text)
        if confirmation:
            return Perception(is_human=True, confirmation_number=confirmation.group(1))

        if _REFUSES_BY_PHONE.search(text):
            return Perception(is_human=True, refuses_by_phone=True)

        dtmf = _DTMF.search(text) or _DTMF_FALLBACK.search(text)
        if dtmf:
            return Perception(dtmf=dtmf.group(1))

        for pattern, concession in _OFFERS:
            if pattern.search(text):
                return Perception(is_human=True, offer=concession)

        if _ASKS_SSN.search(text):
            return Perception(is_human=True, asks_for="ssn")
        if _ASKS_CARD.search(text):
            return Perception(is_human=True, asks_for="full_card_number")
        if _ASKS_VERIFY.search(text):
            return Perception(is_human=True, asks_for="account_verification")

        if _PRESSURE.search(text):
            return Perception(is_human=True, is_pressure=True)

        if _HUMAN.search(text):
            return Perception(is_human=True)

        return Perception()

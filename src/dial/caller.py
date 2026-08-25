"""The call loop.

This is the part that actually does the thing people avoid. It navigates the tree, waits out
the hold, says what it is, states the case, refuses the saves it was not authorized to accept,
and comes back with a confirmation number or an honest failure.

Three decisions in here came straight out of the failure research and are worth naming:

**The model is not consulted while on hold.** A speech-to-speech model pointed at fifteen
minutes of hold music will narrate it, burn the conversation history cap, and produce nothing.
Hold is detected and the brain is gated off until a human is actually there. This is the single
most important engineering detail in the project.

**The transcript belongs to this loop, not to the model.** Nova Sonic silently truncates
history past 200KB. Anything we need later has to be written down here as it happens.

**Every call has a wall-clock deadline.** Misconfigured credentials do not raise on Nova Sonic,
they hang. A call with no deadline is a call that can run forever on a live line.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass, field
from typing import Callable, Protocol

from dial.brain import Brain, Perception
from dial.mandate import NEVER_DISCLOSE, Concession, Mandate, Objective, evaluate_offer

__all__ = ["Outcome", "CallResult", "Line", "place_call", "disclosure_for"]


class Outcome(enum.Enum):
    """How a call ended. There is deliberately no value meaning "probably fine"."""

    OBJECTIVE_MET = "objective_met"
    CONCESSION_ACCEPTED = "concession_accepted"
    REFUSED_BY_PHONE = "refused_by_phone"
    CALLBACK_PROMISED = "callback_promised"
    VOICEMAIL = "voicemail"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    STALLED = "stalled"


#: Outcomes where the objective was not achieved and something else has to happen next.
NEEDS_ESCALATION = frozenset(
    {
        Outcome.REFUSED_BY_PHONE,
        Outcome.CALLBACK_PROMISED,
        Outcome.VOICEMAIL,
        Outcome.DEADLINE_EXCEEDED,
        Outcome.STALLED,
    }
)


class Reply(Protocol):
    text: str
    on_hold: bool
    ended: bool


class Line(Protocol):
    """A phone call in progress. The mock line and the real telephony both satisfy this."""

    def open(self) -> Reply: ...
    def press(self, digits: str) -> Reply: ...
    def say(self, text: str) -> Reply: ...


@dataclass
class CallResult:
    outcome: Outcome
    transcript: list[tuple[str, str]] = field(default_factory=list)
    confirmation_number: str | None = None
    accepted: Concession | None = None
    #: Every offer heard and how the mandate ruled. This is the receipt.
    rulings: list[tuple[str, bool, str]] = field(default_factory=list)
    seconds_elapsed: float = 0.0
    #: True when the brain was asked to interpret hold music. Should always be False.
    consulted_brain_on_hold: bool = False

    @property
    def needs_escalation(self) -> bool:
        return self.outcome in NEEDS_ESCALATION


def disclosure_for(mandate: Mandate) -> str:
    """The opening line, spoken before anything substantive.

    Compliance guidance converges on disclosing an AI voice up front, and the pending FCC
    rulemaking would make it explicit. Recording consent rides along because many states
    require every party to agree. This is not optional and the mandate cannot waive it.
    """
    return (
        f"Before we start, I should tell you that this call uses an AI voice. "
        f"I am an automated assistant calling on behalf of {mandate.principal}, "
        f"who has authorized me to handle this. "
        f"This call may be recorded. Is that alright with you?"
    )


def place_call(
    line: Line,
    mandate: Mandate,
    *,
    brain: Brain,
    now: dt.datetime,
    max_seconds: float = 900.0,
    max_hold_seconds: float = 600.0,
    seconds_per_turn: float = 6.0,
    clock: Callable[[], float] | None = None,
) -> CallResult:
    """Run one call to completion, or to an honest failure.

    `clock` exists so the deadline is testable without waiting fifteen minutes. When it is
    None the loop advances a synthetic clock by `seconds_per_turn` per exchange, which is
    what the tests use and what keeps this deterministic.
    """
    mandate.assert_live(now)

    result = CallResult(outcome=Outcome.STALLED)
    elapsed = 0.0
    hold_elapsed = 0.0
    disclosed = False
    stated = False

    def tick() -> float:
        nonlocal elapsed
        if clock is not None:
            elapsed = clock()
        else:
            elapsed += seconds_per_turn
        return elapsed

    def record(who: str, text: str) -> None:
        # Owned here, not reconstructed from model state, which silently truncates.
        result.transcript.append((who, text))

    def speak(text: str) -> Reply:
        record("dial", text)
        reply = line.say(text)
        record("line", reply.text)
        return reply

    reply = line.open()
    record("line", reply.text)

    while True:
        if tick() > max_seconds:
            result.outcome = Outcome.DEADLINE_EXCEEDED
            break

        # The hold gate. The brain is not consulted here, on purpose: hold music is not
        # speech worth interpreting and interpreting it is how the context cap gets burned.
        if getattr(reply, "on_hold", False):
            hold_elapsed += seconds_per_turn
            if hold_elapsed > max_hold_seconds:
                result.outcome = Outcome.DEADLINE_EXCEEDED
                break
            record("dial", "[waiting on hold, model idle]")
            reply = line.say("")
            record("line", reply.text)
            continue

        perception = brain.perceive(reply.text)

        # A belt-and-braces check on the gate above: if the brain still reports hold after
        # the line said it was not holding, we were consulted on hold music. Flagged rather
        # than silently tolerated, because this is the expensive failure.
        if perception.is_hold:
            result.consulted_brain_on_hold = True
            record("dial", "[waiting on hold, model idle]")
            reply = line.say("")
            record("line", reply.text)
            continue

        hold_elapsed = 0.0

        if perception.is_voicemail:
            result.outcome = Outcome.VOICEMAIL
            break

        if perception.confirmation_number:
            result.outcome = Outcome.OBJECTIVE_MET
            result.confirmation_number = perception.confirmation_number
            break

        if perception.refuses_by_phone:
            result.outcome = Outcome.REFUSED_BY_PHONE
            break

        # Only now is it safe to notice the line hung up. The last thing said on a call is
        # usually the thing worth keeping (a confirmation number, a refusal), so perception
        # always runs before the loop honours `ended`.
        if getattr(reply, "ended", False):
            break

        if perception.dtmf:
            record("dial", f"[presses {perception.dtmf}]")
            reply = line.press(perception.dtmf)
            record("line", reply.text)
            continue

        if perception.offer is not None:
            reply = _handle_offer(perception.offer, mandate, now, result, speak)
            if result.outcome is not Outcome.STALLED:
                break
            continue

        if perception.asks_for in NEVER_DISCLOSE:
            # Asked for something that is never said out loud on a call, even when the
            # human supplied it. Verifying identity is the vendor's problem to solve
            # another way.
            reply = speak(
                "I am not able to share that over the phone. I can confirm the name on "
                "the account and the billing postal code."
            )
            continue

        if perception.is_human and not disclosed:
            disclosed = True
            reply = speak(disclosure_for(mandate))
            continue

        if perception.is_human and not stated:
            stated = True
            reply = speak(_purpose(mandate))
            continue

        # Pressure, verification requests, and anything else: hold the line and restate.
        reply = speak(_restate(mandate))

    result.seconds_elapsed = elapsed
    return result


def _handle_offer(
    offer: Concession,
    mandate: Mandate,
    now: dt.datetime,
    result: CallResult,
    speak: Callable[[str], Reply],
) -> Reply:
    """Rule on a counter-offer and say the corresponding thing."""
    ruling = evaluate_offer(mandate, offer, now=now)
    result.rulings.append((offer.value, ruling.accept, ruling.reason))

    if ruling.accept and offer is Concession.CALLBACK_LATER:
        result.outcome = Outcome.CALLBACK_PROMISED
        return speak("That works, thank you. Please have them call back.")

    if ruling.accept:
        result.outcome = Outcome.CONCESSION_ACCEPTED
        result.accepted = offer
        return speak("Yes, that works. Please apply that and send written confirmation.")

    return speak(
        "No thank you. I am not authorized to accept that. "
        f"Please proceed with the {_noun(mandate)}."
    )


#: How each objective is said out loud, as a verb phrase and as a noun. Generated phrasing
#: reads like a robot ("proceed with the cancel"), and this is the sentence a retention
#: agent actually hears, so it is written rather than derived.
_PHRASING: dict[Objective, tuple[str, str]] = {
    Objective.CANCEL: ("cancel the account", "cancellation"),
    Objective.REFUND: ("request a refund on the account", "refund"),
    Objective.DOWNGRADE: ("downgrade the plan", "downgrade"),
    Objective.DISPUTE: ("dispute a charge on the account", "dispute"),
    Objective.RESTORE_PRIOR_PRICE: (
        "restore the previous rate on the account",
        "rate correction",
    ),
}


def _verb(mandate: Mandate) -> str:
    return _PHRASING[mandate.objective][0]


def _noun(mandate: Mandate) -> str:
    return _PHRASING[mandate.objective][1]


def _purpose(mandate: Mandate) -> str:
    return (
        f"I am calling to {_verb(mandate)} with {mandate.vendor} "
        f"for {mandate.principal}."
    )


def _restate(mandate: Mandate) -> str:
    return f"I understand. Please go ahead and {_verb(mandate)}."

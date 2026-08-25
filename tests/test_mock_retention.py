"""The counterparty behaves like a real retention desk, and the mandate survives it.

The second class here is the one that matters: it wires the mock line to the mandate gate
and proves that an agent following the rules refuses every save and still gets the
cancellation. That is the claim the whole project rests on, tested without AWS, without a
model, and without calling anybody.
"""

from __future__ import annotations

import datetime as dt

from dial.mandate import (
    Concession,
    Mandate,
    Objective,
    evaluate_offer,
)
from dial.mock_retention import CONFIRMATION_PREFIX, RetentionLine, Stage

NOW = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc)


def a_mandate(**overrides: object) -> Mandate:
    base: dict[str, object] = dict(
        principal="Aria Han",
        vendor="Anytime Fitness",
        objective=Objective.CANCEL,
        approved_at=NOW - dt.timedelta(minutes=5),
        expires_at=NOW + dt.timedelta(hours=1),
        acceptable=frozenset(),
    )
    base.update(overrides)
    return Mandate(**base)  # type: ignore[arg-type]


class TestTheLineBehavesLikeARealOne:
    def test_talking_at_an_ivr_gets_nowhere(self) -> None:
        line = RetentionLine()
        line.open()
        reply = line.say("I would like to cancel my membership please")
        assert reply.stage is Stage.IVR
        assert reply.expects_dtmf

    def test_a_wrong_key_does_not_advance(self) -> None:
        line = RetentionLine()
        line.open()
        assert line.press("7").stage is Stage.IVR

    def test_the_right_key_lands_you_on_hold(self) -> None:
        line = RetentionLine()
        line.open()
        reply = line.press("3")
        assert reply.stage is Stage.HOLD and reply.on_hold

    def test_hold_ends_with_a_human(self) -> None:
        line = RetentionLine(hold_turns=3)
        line.open()
        line.press("3")
        replies = [line.say("") for _ in range(3)]
        assert replies[0].on_hold and replies[1].on_hold
        assert replies[-1].stage is Stage.GREETING
        assert not replies[-1].on_hold

    def test_the_save_ladder_escalates_and_ends_in_cancellation(self) -> None:
        line = reach_agent()
        line.say("I want to cancel my membership")
        line.say("Yes, Aria Han, last four 4471")
        for _ in range(len(line.offered), 6):
            line.say("No thank you, please proceed with the cancellation")
        assert line.stage is Stage.CANCELLED

    def test_a_vendor_that_refuses_by_phone_says_so(self) -> None:
        line = reach_agent(refuses_by_phone=True)
        line.say("I want to cancel my membership")
        reply = line.say("Aria Han, last four 4471")
        assert reply.stage is Stage.REFUSED_BY_PHONE
        assert reply.ended


def reach_agent(**kwargs: object) -> RetentionLine:
    """Get past the IVR and the hold queue to a person."""
    line = RetentionLine(**kwargs)  # type: ignore[arg-type]
    line.open()
    line.press("3")
    while line.stage is Stage.HOLD:
        line.say("")
    return line


class TestTheMandateSurvivesTheCall:
    """A full call, every save refused, driven only by the mandate gate."""

    def run_call(self, mandate: Mandate) -> RetentionLine:
        line = reach_agent()
        line.say("I am calling to cancel the membership for Aria Han")
        line.say("Aria Han, last four 4471")

        # Walk the ladder. Each offer is ruled on by the mandate, never by vibes.
        while line.stage is Stage.SAVE and not line.transcript[-1][1].startswith("Alright"):
            offered = line.offered[-1]
            try:
                concession = Concession(offered)
            except ValueError:
                # "guilt" is not an offer, it is pressure. There is nothing to accept.
                line.say("I understand. Please proceed with the cancellation.")
                continue

            ruling = evaluate_offer(mandate, concession, now=NOW)
            if ruling.accept:
                line.say("Yes, that works.")
                break
            line.say("No thank you. Please proceed with the cancellation.")
        return line

    def test_an_empty_mandate_refuses_everything_and_still_cancels(self) -> None:
        line = self.run_call(a_mandate())
        assert line.stage is Stage.CANCELLED, "refusing every save must still end in a cancel"
        confirmation = line.transcript[-1][1]
        assert CONFIRMATION_PREFIX in confirmation

    def test_the_pause_offer_was_actually_made_and_actually_refused(self) -> None:
        """Guards against the test passing because the offer never happened."""
        line = self.run_call(a_mandate())
        assert "pause_instead_of_cancel" in line.offered
        assert line.stage is Stage.CANCELLED

    def test_a_mandate_that_allows_a_discount_takes_it_and_does_not_cancel(self) -> None:
        """The mandate is load bearing in both directions, so prove the other one too."""
        mandate = a_mandate(acceptable=frozenset({Concession.DISCOUNT_OFFER}))
        line = self.run_call(mandate)
        assert line.stage is not Stage.CANCELLED
        assert line.offered[0] == "discount_offer"


class TestDeterminism:
    """The demo has to be repeatable, so the same call must produce the same call."""

    def test_the_confirmation_number_is_stable_across_runs(self) -> None:
        def cancel() -> str:
            line = reach_agent()
            line.say("I want to cancel my membership")
            line.say("Aria Han, last four 4471")
            reply = None
            while line.stage is Stage.SAVE:
                reply = line.say("No thank you, please proceed with the cancellation")
            assert reply is not None and reply.confirmation_number
            return reply.confirmation_number

        assert cancel() == cancel()

    def test_different_vendors_get_different_confirmation_numbers(self) -> None:
        def cancel(vendor: str) -> str:
            line = reach_agent(vendor=vendor)
            line.say("I want to cancel my membership")
            line.say("Aria Han, last four 4471")
            reply = None
            while line.stage is Stage.SAVE:
                reply = line.say("No thank you, please proceed with the cancellation")
            assert reply is not None and reply.confirmation_number
            return reply.confirmation_number

        assert cancel("Anytime Fitness") != cancel("Comcast")

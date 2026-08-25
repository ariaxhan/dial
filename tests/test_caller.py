"""The call loop, tested against a counterparty that fights back.

The first test class is the important one. Consulting the model on hold music is the expensive
failure the research turned up, and it is invisible when it happens, so it is asserted directly
rather than hoped for.
"""

from __future__ import annotations

import datetime as dt

import pytest

from dial.brain import Brain, Perception, ScriptedBrain
from dial.caller import Outcome, disclosure_for, place_call
from dial.mandate import Concession, Mandate, Objective
from dial.mock_retention import RetentionLine

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


class CountingBrain:
    """Wraps a brain and records every utterance it was asked to interpret."""

    def __init__(self, inner: Brain) -> None:
        self.inner = inner
        self.heard: list[str] = []

    def perceive(self, utterance: str) -> Perception:
        self.heard.append(utterance)
        return self.inner.perceive(utterance)


class TestTheHoldGate:
    """The model must not be consulted while hold music is playing."""

    def test_the_brain_never_hears_hold_music(self) -> None:
        brain = CountingBrain(ScriptedBrain())
        line = RetentionLine(hold_turns=8)
        result = place_call(line, a_mandate(), brain=brain, now=NOW)

        assert not result.consulted_brain_on_hold
        assert not any("hold music" in heard.lower() for heard in brain.heard), (
            "the model was asked to interpret hold music, which burns the context cap"
        )

    def test_a_long_hold_still_reaches_a_human(self) -> None:
        line = RetentionLine(hold_turns=20)
        result = place_call(line, a_mandate(), brain=ScriptedBrain(), now=NOW)
        assert result.outcome is Outcome.OBJECTIVE_MET

    def test_the_transcript_still_records_the_wait(self) -> None:
        """Gated off does not mean invisible. The receipt has to show the hold."""
        line = RetentionLine(hold_turns=5)
        result = place_call(line, a_mandate(), brain=ScriptedBrain(), now=NOW)
        waits = [t for who, t in result.transcript if "waiting on hold" in t]
        assert len(waits) >= 4

    def test_an_endless_hold_gives_up_instead_of_hanging(self) -> None:
        line = RetentionLine(hold_turns=10_000)
        result = place_call(
            line, a_mandate(), brain=ScriptedBrain(), now=NOW, max_hold_seconds=60
        )
        assert result.outcome is Outcome.DEADLINE_EXCEEDED


class TestTheFullCall:
    def test_it_cancels_and_comes_back_with_a_confirmation_number(self) -> None:
        line = RetentionLine()
        result = place_call(line, a_mandate(), brain=ScriptedBrain(), now=NOW)

        assert result.outcome is Outcome.OBJECTIVE_MET
        assert result.confirmation_number
        assert result.confirmation_number.startswith("CX-")
        assert not result.needs_escalation

    def test_every_save_was_offered_and_every_save_was_refused(self) -> None:
        line = RetentionLine()
        result = place_call(line, a_mandate(), brain=ScriptedBrain(), now=NOW)

        offered = {name for name, _, _ in result.rulings}
        assert "pause_instead_of_cancel" in offered, "the pause offer must actually happen"
        assert "discount_offer" in offered
        assert all(not accepted for _, accepted, _ in result.rulings)

    def test_the_ai_disclosure_comes_before_the_ask(self) -> None:
        line = RetentionLine()
        result = place_call(line, a_mandate(), brain=ScriptedBrain(), now=NOW)

        said = [text for who, text in result.transcript if who == "dial"]
        disclosure_at = next(
            i for i, t in enumerate(said) if "AI voice" in t
        )
        purpose_at = next(i for i, t in enumerate(said) if "I am calling to" in t)
        assert disclosure_at < purpose_at, "disclosure must precede substantive conversation"

    def test_the_disclosure_names_the_principal_and_asks_about_recording(self) -> None:
        text = disclosure_for(a_mandate())
        assert "AI voice" in text
        assert "Aria Han" in text
        assert "recorded" in text


class TestWhatItWillNotSay:
    def test_it_refuses_to_read_out_a_social_security_number(self) -> None:
        class NosyLine:
            """A line that asks for the one thing that is never said out loud."""

            def __init__(self) -> None:
                self.turns = 0
                self.heard: list[str] = []

            def _reply(self, text: str, **kw: object):
                from dial.mock_retention import Reply, Stage

                return Reply(text=text, stage=Stage.VERIFY, **kw)  # type: ignore[arg-type]

            def open(self):
                return self._reply("Thanks for holding, this is Dana. How can I help?")

            def press(self, digits: str):
                return self._reply("Okay.")

            def say(self, text: str):
                self.heard.append(text)
                self.turns += 1
                if self.turns > 6:
                    return self._reply(
                        "Alright, your confirmation number is CX-999111.", ended=True
                    )
                return self._reply(
                    "For security I need the social security number on the account."
                )

        line = NosyLine()
        result = place_call(line, a_mandate(), brain=ScriptedBrain(), now=NOW)

        said = " ".join(t for who, t in result.transcript if who == "dial").lower()
        assert "not able to share that" in said
        assert "123-45" not in said


class TestOutcomes:
    def test_a_vendor_that_refuses_by_phone_escalates(self) -> None:
        line = RetentionLine(refuses_by_phone=True)
        result = place_call(line, a_mandate(), brain=ScriptedBrain(), now=NOW)
        assert result.outcome is Outcome.REFUSED_BY_PHONE
        assert result.needs_escalation, "this is what forces the certified letter"

    def test_an_authorized_discount_is_accepted_and_recorded(self) -> None:
        mandate = a_mandate(acceptable=frozenset({Concession.DISCOUNT_OFFER}))
        line = RetentionLine()
        result = place_call(line, mandate, brain=ScriptedBrain(), now=NOW)

        assert result.outcome is Outcome.CONCESSION_ACCEPTED
        assert result.accepted is Concession.DISCOUNT_OFFER
        assert result.needs_escalation is False

    def test_an_authorized_callback_counts_as_unfinished(self) -> None:
        mandate = a_mandate(acceptable=frozenset({Concession.CALLBACK_LATER}))
        line = RetentionLine()
        result = place_call(line, mandate, brain=ScriptedBrain(), now=NOW)
        assert result.outcome is Outcome.CALLBACK_PROMISED
        assert result.needs_escalation, "a promised callback resolved nothing"

    def test_an_overall_deadline_is_enforced(self) -> None:
        line = RetentionLine(hold_turns=3)
        result = place_call(
            line, a_mandate(), brain=ScriptedBrain(), now=NOW, max_seconds=12
        )
        assert result.outcome is Outcome.DEADLINE_EXCEEDED

    def test_an_expired_mandate_never_dials(self) -> None:
        from dial.mandate import MandateViolation

        dead = Mandate(
            principal="Aria Han",
            vendor="Anytime Fitness",
            objective=Objective.CANCEL,
            approved_at=NOW - dt.timedelta(hours=3),
            expires_at=NOW - dt.timedelta(hours=1),
        )
        with pytest.raises(MandateViolation):
            place_call(RetentionLine(), dead, brain=ScriptedBrain(), now=NOW)


class TestTheReceipt:
    def test_the_transcript_is_owned_by_the_loop(self) -> None:
        """Nova Sonic silently truncates its own history, so we keep our own."""
        line = RetentionLine()
        result = place_call(line, a_mandate(), brain=ScriptedBrain(), now=NOW)
        speakers = {who for who, _ in result.transcript}
        assert speakers == {"dial", "line"}
        assert len(result.transcript) > 10

    def test_every_offer_carries_the_reason_it_was_refused(self) -> None:
        line = RetentionLine()
        result = place_call(line, a_mandate(), brain=ScriptedBrain(), now=NOW)
        assert result.rulings
        assert all(reason for _, _, reason in result.rulings)


class TestHowItSounds:
    """This is the sentence a retention agent actually hears, so it has to read like English."""

    def test_it_asks_to_proceed_with_the_cancellation_not_the_cancel(self) -> None:
        line = RetentionLine()
        result = place_call(line, a_mandate(), brain=ScriptedBrain(), now=NOW)
        said = " ".join(t for who, t in result.transcript if who == "dial")
        assert "proceed with the cancellation" in said
        assert "proceed with the cancel." not in said

    def test_every_objective_has_written_phrasing(self) -> None:
        from dial.caller import _PHRASING

        for objective in Objective:
            verb, noun = _PHRASING[objective]
            assert verb and noun
            assert "_" not in verb and "_" not in noun

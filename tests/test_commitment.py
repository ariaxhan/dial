"""The commitment gate and the post-call audit.

On Amazon Connect the conversation loop belongs to AWS, so the mandate can no longer be
enforced by generating the agent's words from a ruling. These tests pin the weaker but
honest guarantee that replaces it:

1. A misspoken acceptance cannot become a committed outcome.
2. A misspoken acceptance is detected after the call.

The second class is the one that matters. It simulates exactly the failure the architecture
introduces: an agent that says yes to something it was never authorised to accept.
"""

from __future__ import annotations

import datetime as dt

import pytest

from dial.audit import Severity, audit_call, needs_human_today
from dial.caller import Outcome
from dial.commitment import CommitmentLedger, CommitmentRefused, Receipt
from dial.mandate import Concession, Mandate, Objective

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


def a_ledger(**overrides: object) -> CommitmentLedger:
    return CommitmentLedger(mandate=a_mandate(**overrides), now=NOW, secret="test-secret")


class TestNothingHappensWithoutAReceipt:
    def test_a_fresh_ledger_has_committed_nothing(self) -> None:
        ledger = a_ledger()
        assert not ledger.committed_anything
        assert ledger.outcome_of_record() is None

    def test_a_cancellation_needs_a_confirmation_number(self) -> None:
        """"The agent said it was done" is not evidence; vendors reverse these."""
        with pytest.raises(CommitmentRefused, match="confirmation number"):
            a_ledger().commit(Outcome.OBJECTIVE_MET)

    def test_a_cancellation_with_proof_commits(self) -> None:
        receipt = a_ledger().commit(Outcome.OBJECTIVE_MET, confirmation_number="CX-1")
        assert receipt.outcome == "objective_met"
        assert receipt.confirmation_number == "CX-1"

    def test_outcomes_that_change_nothing_cannot_be_committed(self) -> None:
        for outcome in (Outcome.CALLBACK_PROMISED, Outcome.VOICEMAIL, Outcome.STALLED,
                        Outcome.DEADLINE_EXCEEDED, Outcome.REFUSED_BY_PHONE):
            with pytest.raises(CommitmentRefused):
                a_ledger().commit(outcome, confirmation_number="CX-1")


class TestTheMandateStillGatesConcessions:
    def test_an_unauthorised_concession_cannot_be_committed(self) -> None:
        ledger = a_ledger()
        with pytest.raises(CommitmentRefused, match="pause_instead_of_cancel"):
            ledger.commit(
                Outcome.CONCESSION_ACCEPTED,
                concession=Concession.PAUSE_INSTEAD_OF_CANCEL,
            )
        assert not ledger.committed_anything
        assert ledger.refusals, "the refusal must be recorded for the audit"

    def test_an_authorised_concession_commits(self) -> None:
        ledger = a_ledger(acceptable=frozenset({Concession.DISCOUNT_OFFER}))
        receipt = ledger.commit(
            Outcome.CONCESSION_ACCEPTED, concession=Concession.DISCOUNT_OFFER
        )
        assert receipt.concession == "discount_offer"

    def test_committing_a_concession_without_naming_it_is_refused(self) -> None:
        ledger = a_ledger(acceptable=frozenset({Concession.DISCOUNT_OFFER}))
        with pytest.raises(CommitmentRefused, match="which concession"):
            ledger.commit(Outcome.CONCESSION_ACCEPTED)

    def test_an_expired_mandate_commits_nothing(self) -> None:
        dead = Mandate(
            principal="Aria Han",
            vendor="Anytime Fitness",
            objective=Objective.CANCEL,
            approved_at=NOW - dt.timedelta(hours=3),
            expires_at=NOW - dt.timedelta(hours=1),
        )
        ledger = CommitmentLedger(mandate=dead, now=NOW, secret="test-secret")
        with pytest.raises(CommitmentRefused):
            ledger.commit(Outcome.OBJECTIVE_MET, confirmation_number="CX-1")

    def test_the_spend_ceiling_still_applies(self) -> None:
        ledger = a_ledger(
            acceptable=frozenset({Concession.DOWNGRADE_INSTEAD_OF_CANCEL}),
            max_spend_usd=10.0,
        )
        with pytest.raises(CommitmentRefused, match="ceiling"):
            ledger.commit(
                Outcome.CONCESSION_ACCEPTED,
                concession=Concession.DOWNGRADE_INSTEAD_OF_CANCEL,
                costs_usd=19.99,
            )


class TestReceipts:
    def test_a_receipt_verifies(self) -> None:
        receipt = a_ledger().commit(Outcome.OBJECTIVE_MET, confirmation_number="CX-1")
        assert receipt.verify("test-secret")

    def test_a_tampered_receipt_does_not_verify(self) -> None:
        """The boundary has to be visible enough that a refactor cannot route around it."""
        original = a_ledger().commit(Outcome.OBJECTIVE_MET, confirmation_number="CX-1")
        forged = Receipt(**{**original.unsigned(), "outcome": "concession_accepted"},
                         signature=original.signature)
        assert not forged.verify("test-secret")

    def test_a_receipt_from_another_secret_does_not_verify(self) -> None:
        receipt = a_ledger().commit(Outcome.OBJECTIVE_MET, confirmation_number="CX-1")
        assert not receipt.verify("a-different-secret")


class TestTheAuditCatchesWhatTheGateCannot:
    """The failure the Connect architecture introduces, simulated directly."""

    def test_a_verbal_yes_to_an_unauthorised_pause_is_caught(self) -> None:
        transcript = [
            ("dial", "Before we start, this call uses an AI voice, on behalf of Aria Han."),
            ("dial", "I am calling to cancel the account with Anytime Fitness."),
            ("line", "I can freeze the membership for three months at no charge instead."),
            ("dial", "Yes, that works. Please apply that."),
            ("line", "Done, your membership is frozen."),
        ]
        ledger = a_ledger()          # nothing committed: the gate refused, correctly
        findings = audit_call(transcript, ledger, ledger.mandate)

        top = findings[0]
        assert top.severity is Severity.NEEDS_REVOCATION
        assert top.code == "verbal_acceptance_without_commitment"
        assert "pause_instead_of_cancel" in top.detail
        assert needs_human_today(findings)

    def test_an_authorised_acceptance_that_was_committed_is_not_flagged(self) -> None:
        ledger = a_ledger(acceptable=frozenset({Concession.DISCOUNT_OFFER}))
        ledger.commit(Outcome.CONCESSION_ACCEPTED, concession=Concession.DISCOUNT_OFFER)
        transcript = [
            ("dial", "This call uses an AI voice, on behalf of Aria Han."),
            ("line", "I can offer you fifty percent off for three months."),
            ("dial", "Yes, that works. Please apply that."),
        ]
        findings = audit_call(transcript, ledger, ledger.mandate)
        assert not needs_human_today(findings)

    def test_refusing_an_offer_is_never_flagged(self) -> None:
        transcript = [
            ("dial", "This call uses an AI voice, on behalf of Aria Han."),
            ("line", "I can freeze the membership for three months instead."),
            ("dial", "No thank you. Please proceed with the cancellation."),
        ]
        ledger = a_ledger()
        assert not needs_human_today(audit_call(transcript, ledger, ledger.mandate))

    def test_a_spoken_social_security_number_is_caught(self) -> None:
        transcript = [
            ("dial", "This call uses an AI voice, on behalf of Aria Han."),
            ("dial", "The number is 123-45-6789."),
        ]
        ledger = a_ledger()
        findings = audit_call(transcript, ledger, ledger.mandate)
        assert any(f.code == "spoke_ssn" for f in findings)
        assert needs_human_today(findings)

    def test_a_missing_disclosure_is_flagged(self) -> None:
        transcript = [("dial", "I am calling to cancel the account with Anytime Fitness.")]
        ledger = a_ledger()
        findings = audit_call(transcript, ledger, ledger.mandate)
        assert any(f.code == "no_ai_disclosure" for f in findings)

    def test_disclosure_after_the_ask_is_flagged(self) -> None:
        transcript = [
            ("dial", "I am calling to cancel the account with Anytime Fitness."),
            ("dial", "Also, this call uses an AI voice."),
        ]
        ledger = a_ledger()
        findings = audit_call(transcript, ledger, ledger.mandate)
        assert any(f.code == "disclosure_after_the_ask" for f in findings)

    def test_a_confirmation_number_never_spoken_is_flagged(self) -> None:
        ledger = a_ledger()
        ledger.commit(Outcome.OBJECTIVE_MET, confirmation_number="CX-999")
        transcript = [("dial", "This call uses an AI voice, on behalf of Aria Han.")]
        findings = audit_call(transcript, ledger, ledger.mandate)
        assert any(f.code == "confirmation_not_in_transcript" for f in findings)

    def test_a_clean_call_says_so_explicitly(self) -> None:
        """An audit that produces nothing must still show that it ran."""
        transcript = [
            ("dial", "This call uses an AI voice, on behalf of Aria Han."),
            ("dial", "I am calling to cancel the account with Anytime Fitness."),
            ("line", "Alright, your confirmation number is CX-7."),
        ]
        ledger = a_ledger()
        ledger.commit(Outcome.OBJECTIVE_MET, confirmation_number="CX-7")
        findings = audit_call(transcript, ledger, ledger.mandate)
        assert [f.severity for f in findings] == [Severity.CLEAN]
        assert not needs_human_today(findings)

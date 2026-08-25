"""A retention specialist's job is to move the boundary. These tests are that specialist."""

from __future__ import annotations

import datetime as dt

import pytest

from dial.mandate import (
    Concession,
    Mandate,
    MandateViolation,
    Objective,
    evaluate_offer,
)

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


class TestConstruction:
    def test_expiry_must_follow_approval(self) -> None:
        with pytest.raises(MandateViolation):
            a_mandate(expires_at=NOW - dt.timedelta(hours=2))

    def test_ai_disclosure_cannot_be_waived(self) -> None:
        with pytest.raises(MandateViolation, match="disclosure"):
            a_mandate(disclose_ai=False)

    def test_a_new_contract_term_can_never_be_pre_approved(self) -> None:
        with pytest.raises(MandateViolation):
            a_mandate(acceptable=frozenset({Concession.NEW_CONTRACT_TERM}))

    def test_negative_money_is_refused(self) -> None:
        with pytest.raises(MandateViolation):
            a_mandate(max_spend_usd=-1.0)


class TestExpiry:
    def test_expired_mandate_cannot_be_used(self) -> None:
        mandate = a_mandate(expires_at=NOW - dt.timedelta(seconds=1))
        # Constructed in the past so it is valid, but dead by NOW.
        mandate = Mandate(
            principal="Aria Han",
            vendor="Anytime Fitness",
            objective=Objective.CANCEL,
            approved_at=NOW - dt.timedelta(hours=2),
            expires_at=NOW - dt.timedelta(hours=1),
        )
        assert not mandate.live_at(NOW)
        with pytest.raises(MandateViolation):
            evaluate_offer(mandate, Concession.DISCOUNT_OFFER, now=NOW)


class TestTheOfferGate:
    def test_unapproved_concession_is_refused(self) -> None:
        ruling = evaluate_offer(a_mandate(), Concession.PAUSE_INSTEAD_OF_CANCEL, now=NOW)
        assert not ruling.accept
        assert "not approved" in ruling.reason

    def test_approved_concession_is_accepted(self) -> None:
        mandate = a_mandate(acceptable=frozenset({Concession.DISCOUNT_OFFER}))
        assert evaluate_offer(mandate, Concession.DISCOUNT_OFFER, now=NOW).accept

    def test_a_pause_offered_instead_of_a_cancel_is_refused_by_default(self) -> None:
        """The single most common retention save, and the default answer is no."""
        ruling = evaluate_offer(a_mandate(), Concession.PAUSE_INSTEAD_OF_CANCEL, now=NOW)
        assert not ruling.accept

    def test_spend_ceiling_is_enforced(self) -> None:
        mandate = a_mandate(
            acceptable=frozenset({Concession.DOWNGRADE_INSTEAD_OF_CANCEL}),
            max_spend_usd=10.0,
        )
        ruling = evaluate_offer(
            mandate,
            Concession.DOWNGRADE_INSTEAD_OF_CANCEL,
            now=NOW,
            costs_usd=19.99,
        )
        assert not ruling.accept
        assert "ceiling" in ruling.reason

    def test_refund_floor_is_enforced(self) -> None:
        mandate = a_mandate(
            objective=Objective.REFUND,
            acceptable=frozenset({Concession.PARTIAL_REFUND}),
            min_acceptable_refund_usd=50.0,
        )
        low = evaluate_offer(mandate, Concession.PARTIAL_REFUND, now=NOW, amount_usd=20.0)
        assert not low.accept
        ok = evaluate_offer(mandate, Concession.PARTIAL_REFUND, now=NOW, amount_usd=75.0)
        assert ok.accept

    def test_callback_is_accepted_but_escalates(self) -> None:
        mandate = a_mandate(acceptable=frozenset({Concession.CALLBACK_LATER}))
        ruling = evaluate_offer(mandate, Concession.CALLBACK_LATER, now=NOW)
        assert ruling.accept and ruling.escalate


class TestWidening:
    """The attack is a mandate that grows during a call. It must not be possible."""

    def test_cannot_add_acceptable_concessions(self) -> None:
        with pytest.raises(MandateViolation):
            a_mandate().narrowed(acceptable=frozenset({Concession.DISCOUNT_OFFER}))

    def test_cannot_raise_the_spend_ceiling(self) -> None:
        with pytest.raises(MandateViolation):
            a_mandate(max_spend_usd=5.0).narrowed(max_spend_usd=500.0)

    def test_cannot_extend_expiry(self) -> None:
        with pytest.raises(MandateViolation):
            a_mandate().narrowed(expires_at=NOW + dt.timedelta(days=30))

    def test_cannot_retarget_to_another_vendor(self) -> None:
        with pytest.raises(MandateViolation):
            a_mandate().narrowed(vendor="Some Other Company")

    def test_narrowing_is_allowed(self) -> None:
        mandate = a_mandate(
            acceptable=frozenset(
                {Concession.DISCOUNT_OFFER, Concession.PARTIAL_REFUND}
            ),
            max_spend_usd=20.0,
        )
        tighter = mandate.narrowed(
            acceptable=frozenset({Concession.DISCOUNT_OFFER}), max_spend_usd=5.0
        )
        assert tighter.acceptable == frozenset({Concession.DISCOUNT_OFFER})
        assert tighter.max_spend_usd == 5.0

    def test_mandate_is_frozen(self) -> None:
        with pytest.raises(Exception):
            a_mandate().max_spend_usd = 1000.0  # type: ignore[misc]

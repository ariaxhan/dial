"""Reading a mailbox without being fooled by it.

Marketing mail is built to look like a receipt: a vendor, a dollar amount, an urgent date.
Most of these tests are about refusing to extract from it, because a false charge becomes a
false subscription becomes a phone call about something that never happened.
"""

from __future__ import annotations

import datetime as dt

import pytest

from dial.ingest.mailbox import (
    Message,
    extract,
    extract_charges,
    extract_signals,
    vendor_from_sender,
)
from dial.leaks import SignalKind

DAY = dt.date(2026, 3, 14)


def msg(sender: str, subject: str, body: str = "", *, id: str = "m1",
        date: dt.date = DAY) -> Message:
    return Message(id=id, sender=sender, subject=subject, date=date, body=body)


class TestVendorFromSender:
    @pytest.mark.parametrize(
        "sender,expected",
        [
            ("billing@anytimefitness.com", "Anytime Fitness"),
            ("no-reply@comcast.com", "Comcast"),
            ("Netflix <info@netflix.com>", "Netflix"),
            ("receipts@calm.com", "Calm"),
            ("noreply@e.anytimefitness.com", "Anytime Fitness"),
            ("notifications@mail.dropbox.com", "Dropbox"),
        ],
    )
    def test_the_domain_identifies_the_vendor(self, sender: str, expected: str) -> None:
        assert vendor_from_sender(sender) == expected

    def test_a_personal_mailbox_is_not_a_company_called_gmail(self) -> None:
        """Forwarded mail arrives from a person, and Gmail is not a vendor."""
        assert vendor_from_sender("friend@gmail.com", subject="Comcast receipt") == "Comcast"
        assert vendor_from_sender("friend@gmail.com") == ""

    def test_junk_senders_produce_nothing(self) -> None:
        assert vendor_from_sender("") == ""
        assert vendor_from_sender("not an address") == ""


class TestNotBeingFooled:
    def test_a_marketing_email_with_a_dollar_amount_is_not_a_charge(self) -> None:
        m = msg(
            "deals@anytimefitness.com",
            "Save $50 when you upgrade today",
            "Limited time offer. Save $50.00 on an annual plan. Shop now.",
        )
        assert extract_charges([m]) == []

    def test_an_amount_with_no_receipt_language_is_not_a_charge(self) -> None:
        m = msg("hello@calm.com", "Your week in review", "You have $10.00 in credits.")
        assert extract_charges([m]) == []

    def test_a_bare_amount_with_no_context_word_is_not_a_charge(self) -> None:
        m = msg("billing@calm.com", "Receipt", "Thanks for your payment. $14.99")
        # No "total" or "amount charged" adjacent to the number, so nothing is extracted.
        assert extract_charges([m]) == []

    def test_being_billed_is_not_evidence_that_you_used_it(self) -> None:
        """The whole zombie finding depends on this distinction."""
        m = msg(
            "billing@anytimefitness.com",
            "Your monthly receipt",
            "Payment received. Total: $49.00. Your workout summary is attached.",
        )
        kinds = {s.kind for s in extract_signals([m])}
        assert SignalKind.ENGAGEMENT not in kinds


class TestCharges:
    def test_a_receipt_becomes_a_charge(self) -> None:
        m = msg(
            "billing@anytimefitness.com",
            "Your receipt from Anytime Fitness",
            "Thanks for your payment.\nTotal: $49.00\nBilled monthly.",
        )
        charges = extract_charges([m])
        assert len(charges) == 1
        assert charges[0].vendor == "Anytime Fitness"
        assert charges[0].amount_usd == 49.00
        assert charges[0].category == "gym"
        assert charges[0].date == DAY
        assert charges[0].source_id == "mail:m1"

    def test_thousands_separators_survive(self) -> None:
        m = msg("billing@delta.com", "Your receipt", "Order total: $1,412.30")
        assert extract_charges([m])[0].amount_usd == 1412.30

    def test_a_refund_is_a_credit(self) -> None:
        m = msg(
            "returns@wayfair.com",
            "Your refund has been issued",
            "We have refunded your order. Amount paid: $88.00 back to your card.",
        )
        charges = extract_charges([m])
        assert len(charges) == 1
        assert charges[0].is_credit

    def test_a_promotional_receipt_still_counts(self) -> None:
        """Marketing language inside a genuine receipt must not suppress it."""
        m = msg(
            "billing@comcast.com",
            "Your payment confirmation",
            "Payment received. Total charged: $129.99. Upgrade now and save $20!",
        )
        assert len(extract_charges([m])) == 1


class TestSignals:
    def test_a_trial_welcome_is_a_trial_signal(self) -> None:
        m = msg("hello@calm.com", "Your 30 day free trial has begun", "Enjoy.")
        signals = extract_signals([m])
        assert [s.kind for s in signals] == [SignalKind.TRIAL_STARTED]
        assert signals[0].vendor == "Calm"

    def test_a_return_confirmation_is_a_return_signal(self) -> None:
        m = msg("returns@wayfair.com", "We have received your return", "")
        assert SignalKind.RETURN_CONFIRMED in {s.kind for s in extract_signals([m])}

    def test_a_price_change_notice_is_captured(self) -> None:
        m = msg("no-reply@comcast.com", "An update to your monthly rate", "")
        assert SignalKind.PRICE_CHANGE_NOTICE in {s.kind for s in extract_signals([m])}

    def test_real_use_is_an_engagement_signal(self) -> None:
        for subject in [
            "Your check-in confirmation",
            "Continue watching tonight",
            "A file was shared with you",
            "New sign-in from a new device",
        ]:
            m = msg("no-reply@netflix.com", subject, "")
            assert SignalKind.ENGAGEMENT in {s.kind for s in extract_signals([m])}, subject

    def test_one_message_can_carry_two_signals(self) -> None:
        m = msg(
            "hello@calm.com",
            "Your free trial has begun and will automatically renew",
            "",
        )
        kinds = {s.kind for s in extract_signals([m])}
        assert SignalKind.TRIAL_STARTED in kinds
        assert SignalKind.RENEWAL_NOTICE in kinds

    def test_ordinary_mail_produces_no_signals(self) -> None:
        m = msg("friend@example.com", "lunch tomorrow?", "are you free")
        assert extract_signals([m]) == []


class TestEndToEnd:
    def test_a_mailbox_produces_the_converted_trial_finding(self) -> None:
        """Mail is the only source that knows a trial started, so prove the chain works."""
        from dial.leaks import LeakKind, detect

        messages = [
            msg("hello@calm.com", "Your 30 day free trial has begun", "", id="t0",
                date=dt.date(2026, 2, 2)),
        ]
        for i in range(5):
            messages.append(
                msg(
                    "billing@calm.com",
                    "Your receipt from Calm",
                    "Payment received. Total: $14.99",
                    id=f"c{i}",
                    date=dt.date(2026, 3, 2) + dt.timedelta(days=30 * i),
                )
            )
        # Something else in the mailbox shows real use, so the detector is allowed to
        # reason about what is unused.
        messages.append(
            msg("no-reply@netflix.com", "Continue watching", "", id="e0",
                date=dt.date(2026, 8, 20))
        )

        charges, signals = extract(messages)
        leaks = detect(charges, signals, today=dt.date(2026, 9, 1))

        trials = [leak for leak in leaks if leak.kind is LeakKind.CONVERTED_TRIAL]
        assert len(trials) == 1
        assert trials[0].vendor == "Calm"
        assert trials[0].annual_usd == 179.88

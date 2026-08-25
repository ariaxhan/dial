"""Detection proposes and must not over-propose. False positives are the real failure."""

from __future__ import annotations

import datetime as dt

from dial.leaks import (
    Charge,
    LeakKind,
    Route,
    Signal,
    SignalKind,
    annual_cost,
    detect,
)

TODAY = dt.date(2026, 9, 1)


def monthly(vendor: str, amount: float, months: int, *, category: str | None = None,
            start: dt.date = dt.date(2026, 2, 1)) -> list[Charge]:
    return [
        Charge(
            vendor=vendor,
            amount_usd=amount,
            date=start + dt.timedelta(days=30 * i),
            source_id=f"{vendor}-{i}",
            category=category,
        )
        for i in range(months)
    ]


class TestQuiet:
    def test_no_charges_finds_nothing(self) -> None:
        assert detect([], [], today=TODAY) == []

    def test_a_single_charge_is_not_a_subscription(self) -> None:
        charges = [Charge("Corner Store", 12.40, dt.date(2026, 8, 1), "s1")]
        assert detect(charges, [], today=TODAY) == []

    def test_irregular_charges_are_not_a_cadence(self) -> None:
        charges = [
            Charge("Corner Store", 12.40, dt.date(2026, 3, 1), "s1"),
            Charge("Corner Store", 3.10, dt.date(2026, 5, 19), "s2"),
            Charge("Corner Store", 44.00, dt.date(2026, 8, 2), "s3"),
        ]
        found = [leak for leak in detect(charges, [], today=TODAY)
                 if leak.kind is not LeakKind.ZOMBIE_SERVICE]
        assert found == []

    def test_varying_amounts_on_a_monthly_cadence_are_a_shop_not_a_subscription(self) -> None:
        """Regular spacing alone is not a subscription. The amounts have to hold."""
        charges = [
            Charge("Corner Store", 12.40, dt.date(2026, 3, 1), "s1"),
            Charge("Corner Store", 3.10, dt.date(2026, 3, 31), "s2"),
            Charge("Corner Store", 44.00, dt.date(2026, 4, 30), "s3"),
            Charge("Corner Store", 8.75, dt.date(2026, 5, 30), "s4"),
        ]
        kinds = {leak.kind for leak in detect(charges, [], today=TODAY)}
        assert LeakKind.SILENT_PRICE_RISE not in kinds
        assert LeakKind.ZOMBIE_SERVICE not in kinds

    def test_a_cadence_matching_no_real_billing_period_is_rejected(self) -> None:
        """Nothing legitimate bills every 77 days."""
        charges = [
            Charge("Odd Vendor", 20.00, dt.date(2026, 1, 1), "o1"),
            Charge("Odd Vendor", 20.00, dt.date(2026, 3, 19), "o2"),
            Charge("Odd Vendor", 20.00, dt.date(2026, 6, 4), "o3"),
        ]
        assert detect(charges, [], today=TODAY) == []

    def test_a_steady_used_subscription_is_not_flagged_as_zombie(self) -> None:
        charges = monthly("Spotify", 11.99, 6, category="music")
        signals = [
            Signal("Spotify", SignalKind.ENGAGEMENT, TODAY - dt.timedelta(days=3), "e1")
        ]
        kinds = {leak.kind for leak in detect(charges, signals, today=TODAY)}
        assert LeakKind.ZOMBIE_SERVICE not in kinds


class TestPriceRise:
    def test_a_silent_rise_is_caught_and_priced(self) -> None:
        charges = monthly("Comcast", 89.99, 4, category="cable")
        charges += [
            Charge("Comcast", 129.99, dt.date(2026, 6, 2), "Comcast-4", "cable"),
            Charge("Comcast", 129.99, dt.date(2026, 7, 2), "Comcast-5", "cable"),
        ]
        leaks = [leak for leak in detect(charges, [], today=TODAY)
                 if leak.kind is LeakKind.SILENT_PRICE_RISE]
        assert len(leaks) == 1
        leak = leaks[0]
        assert leak.vendor == "Comcast"
        assert leak.monthly_usd == 40.0
        assert leak.annual_usd == 480.0
        assert leak.route is Route.PHONE_REQUIRED  # cable retention lives on the phone
        assert "89.99" in leak.rationale and "129.99" in leak.rationale

    def test_a_trivial_rise_is_ignored(self) -> None:
        charges = monthly("Netflix", 15.49, 5)
        charges.append(Charge("Netflix", 15.99, dt.date(2026, 7, 1), "Netflix-5"))
        kinds = {leak.kind for leak in detect(charges, [], today=TODAY)}
        assert LeakKind.SILENT_PRICE_RISE not in kinds


class TestConvertedTrial:
    def test_trial_then_charge_is_caught(self) -> None:
        signals = [Signal("Calm", SignalKind.TRIAL_STARTED, dt.date(2026, 1, 18), "t1")]
        charges = monthly("Calm", 14.99, 5, start=dt.date(2026, 2, 1))
        leaks = [leak for leak in detect(charges, signals, today=TODAY)
                 if leak.kind is LeakKind.CONVERTED_TRIAL]
        assert len(leaks) == 1
        assert leaks[0].confidence >= 0.8  # no engagement recorded
        assert "free trial" in leaks[0].rationale

    def test_a_charge_long_after_a_trial_is_not_a_conversion(self) -> None:
        signals = [Signal("Calm", SignalKind.TRIAL_STARTED, dt.date(2025, 1, 1), "t1")]
        charges = monthly("Calm", 14.99, 5)
        kinds = {leak.kind for leak in detect(charges, signals, today=TODAY)}
        assert LeakKind.CONVERTED_TRIAL not in kinds


class TestDoubleCharge:
    def test_two_identical_charges_on_one_day_go_to_dispute(self) -> None:
        charges = [
            Charge("Delta", 412.30, dt.date(2026, 8, 4), "d1"),
            Charge("Delta", 412.30, dt.date(2026, 8, 4), "d2"),
        ]
        leaks = detect(charges, [], today=TODAY)
        doubles = [leak for leak in leaks if leak.kind is LeakKind.DOUBLE_CHARGE]
        assert len(doubles) == 1
        assert doubles[0].route is Route.CARD_DISPUTE
        assert doubles[0].confidence > 0.9
        assert set(doubles[0].evidence) == {"d1", "d2"}

    def test_same_amount_on_different_days_is_not_a_double(self) -> None:
        charges = [
            Charge("Delta", 412.30, dt.date(2026, 8, 4), "d1"),
            Charge("Delta", 412.30, dt.date(2026, 8, 9), "d2"),
        ]
        kinds = {leak.kind for leak in detect(charges, [], today=TODAY)}
        assert LeakKind.DOUBLE_CHARGE not in kinds


class TestMissingRefund:
    def test_a_return_with_no_credit_is_caught(self) -> None:
        signals = [
            Signal("Wayfair", SignalKind.RETURN_CONFIRMED, dt.date(2026, 6, 1), "r1")
        ]
        leaks = detect([], signals, today=TODAY)
        assert [leak.kind for leak in leaks] == [LeakKind.REFUND_NEVER_ISSUED]
        assert leaks[0].route is Route.CARD_DISPUTE

    def test_a_refunded_return_is_not_flagged(self) -> None:
        signals = [
            Signal("Wayfair", SignalKind.RETURN_CONFIRMED, dt.date(2026, 6, 1), "r1")
        ]
        credits = [Charge("Wayfair", -88.00, dt.date(2026, 6, 9), "c1")]
        assert detect(credits, signals, today=TODAY) == []

    def test_a_recent_return_is_still_inside_the_window(self) -> None:
        signals = [
            Signal("Wayfair", SignalKind.RETURN_CONFIRMED, TODAY - dt.timedelta(days=5), "r1")
        ]
        assert detect([], signals, today=TODAY) == []


class TestZombie:
    def test_an_unused_gym_is_caught_and_routed_to_the_phone(self) -> None:
        charges = monthly("Anytime Fitness", 49.00, 7, category="gym",
                          start=dt.date(2026, 1, 5))
        signals = [
            Signal("Anytime Fitness", SignalKind.ENGAGEMENT, dt.date(2026, 1, 20), "e1")
        ]
        leaks = [leak for leak in detect(charges, signals, today=TODAY)
                 if leak.kind is LeakKind.ZOMBIE_SERVICE]
        assert len(leaks) == 1
        assert leaks[0].route is Route.PHONE_REQUIRED
        assert leaks[0].annual_usd == 588.0


class TestDuplicates:
    def test_two_services_in_one_category_propose_dropping_the_cheaper(self) -> None:
        charges = monthly("Dropbox", 11.99, 5, category="cloud_storage")
        charges += monthly("Google One", 2.99, 5, category="cloud_storage")
        dupes = [leak for leak in detect(charges, [], today=TODAY)
                 if leak.kind is LeakKind.DUPLICATE_SERVICE]
        assert len(dupes) == 1
        assert dupes[0].vendor == "Google One"
        assert dupes[0].confidence < 0.7  # genuinely uncertain, so say so


class TestReporting:
    def test_leaks_are_ranked_by_annual_cost(self) -> None:
        charges = monthly("Comcast", 89.99, 4, category="cable")
        charges += [Charge("Comcast", 129.99, dt.date(2026, 6, 2), "c4", "cable")]
        charges += monthly("Calm", 14.99, 5, start=dt.date(2026, 2, 1))
        leaks = detect(
            charges,
            [Signal("Calm", SignalKind.TRIAL_STARTED, dt.date(2026, 1, 18), "t1")],
            today=TODAY,
        )
        costs = [leak.annual_usd for leak in leaks]
        assert costs == sorted(costs, reverse=True)

    def test_annual_cost_can_count_only_what_a_human_approved(self) -> None:
        charges = monthly("Anytime Fitness", 49.00, 7, category="gym",
                          start=dt.date(2026, 1, 5))
        signals = [
            Signal("Anytime Fitness", SignalKind.ENGAGEMENT, dt.date(2026, 1, 20), "e1")
        ]
        leaks = detect(charges, signals, today=TODAY)
        assert annual_cost(leaks) > 0
        assert annual_cost(leaks, approved_only=True) == 0.0


class TestRealWorldCadence:
    """Real billing lands on a day of the month, so gaps run 28 to 31, not a clean 30."""

    def test_billing_on_the_fifth_of_each_month_is_still_a_subscription(self) -> None:
        charges = [
            Charge("Anytime Fitness", 49.00, dt.date(2026, m, 5), f"g{m}", "gym")
            for m in (1, 2, 3, 4, 5, 6, 7, 8)
        ]
        signals = [
            Signal("Anytime Fitness", SignalKind.ENGAGEMENT, dt.date(2026, 1, 20), "e1")
        ]
        leaks = detect(charges, signals, today=TODAY)
        assert any(leak.kind is LeakKind.ZOMBIE_SERVICE for leak in leaks), (
            "February makes the gaps uneven; the detector must survive that"
        )

    def test_annual_renewal_is_detected(self) -> None:
        charges = [
            Charge("SomeDomain", 220.00, dt.date(2022, 3, 2), "y1", "domains"),
            Charge("SomeDomain", 220.00, dt.date(2023, 3, 1), "y2", "domains"),
            Charge("SomeDomain", 220.00, dt.date(2024, 3, 3), "y3", "domains"),
            Charge("SomeDomain", 310.00, dt.date(2025, 3, 2), "y4", "domains"),
        ]
        rises = [leak for leak in detect(charges, [], today=TODAY)
                 if leak.kind is LeakKind.SILENT_PRICE_RISE]
        assert len(rises) == 1
        assert rises[0].monthly_usd == 7.4  # $90 a year, expressed monthly


class TestHonestClaims:
    """The detector must not claim more than its evidence supports."""

    def test_without_usage_evidence_it_never_claims_a_service_is_unused(self) -> None:
        """A statement alone cannot show whether you use Netflix."""
        charges = monthly("Netflix", 15.49, 8, category="streaming")
        leaks = detect(charges, [], today=TODAY)
        assert not any(leak.kind is LeakKind.ZOMBIE_SERVICE for leak in leaks)

    def test_with_usage_evidence_it_will_make_the_claim(self) -> None:
        charges = monthly("Netflix", 15.49, 8, category="streaming")
        signals = [Signal("Spotify", SignalKind.ENGAGEMENT, dt.date(2026, 8, 1), "e1")]
        leaks = detect(charges, signals, today=TODAY)
        assert any(leak.kind is LeakKind.ZOMBIE_SERVICE for leak in leaks)

    def test_a_double_charge_is_not_annualised(self) -> None:
        charges = [
            Charge("Delta", 412.30, dt.date(2026, 8, 4), "d1"),
            Charge("Delta", 412.30, dt.date(2026, 8, 4), "d2"),
        ]
        leak = detect(charges, [], today=TODAY)[0]
        assert leak.annual_usd == 0.0
        assert leak.one_time_usd == 412.30
        assert leak.impact_usd == 412.30

    def test_one_vendor_is_not_counted_twice_in_the_total(self) -> None:
        """Comcast as both a price rise and a zombie is one problem, described twice."""
        charges = monthly("Comcast", 89.99, 4, category="cable")
        charges += [
            Charge("Comcast", 129.99, dt.date(2026, 6, 2), "c4", "cable"),
            Charge("Comcast", 129.99, dt.date(2026, 7, 2), "c5", "cable"),
        ]
        signals = [Signal("Spotify", SignalKind.ENGAGEMENT, dt.date(2026, 8, 1), "e1")]
        leaks = detect(charges, signals, today=TODAY)
        comcast = [leak for leak in leaks if leak.vendor == "Comcast"]
        assert len(comcast) == 1, "the vaguer finding must be suppressed"
        assert comcast[0].kind is LeakKind.SILENT_PRICE_RISE
        assert annual_cost(leaks) == comcast[0].annual_usd

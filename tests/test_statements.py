"""Statement parsing, where the interesting failures are silent rather than loud."""

from __future__ import annotations

import datetime as dt

import pytest

from dial.ingest.statements import (
    guess_category,
    normalize_vendor,
    parse_csv,
)


class TestVendorNormalization:
    @pytest.mark.parametrize(
        "descriptor",
        [
            "SQ *ANYTIME FITNESS 4471",
            "POS DEBIT ANYTIME FITNESS #221",
            "RECURRING DEBIT CARD ANYTIME FITNESS",
            "ANYTIME FITNESS INC",
            "  anytime   fitness  ",
            "PURCHASE AUTHORIZED ON 03/14 ANYTIME FITNESS CA",
        ],
    )
    def test_descriptor_variants_collapse_to_one_vendor(self, descriptor: str) -> None:
        assert normalize_vendor(descriptor) == "Anytime Fitness"

    def test_a_cadence_is_only_visible_after_normalization(self) -> None:
        """The whole reason this module exists."""
        variants = ["SQ *ANYTIME FITNESS 4471", "ANYTIME FITNESS INC", "anytime fitness"]
        assert len({normalize_vendor(v) for v in variants}) == 1

    def test_empty_and_junk_descriptors_are_dropped(self) -> None:
        assert normalize_vendor("") == ""
        assert normalize_vendor("   ") == ""

    def test_distinct_vendors_are_not_merged(self) -> None:
        """Over-merging invents a cadence, which is worse than missing one."""
        assert normalize_vendor("PLANET FITNESS") != normalize_vendor("ANYTIME FITNESS")


class TestCategories:
    def test_known_vendors_get_a_category(self) -> None:
        assert guess_category("Anytime Fitness") == "gym"
        assert guess_category("Comcast") == "cable"
        assert guess_category("Geico") == "insurance"

    def test_unknown_vendors_return_none_rather_than_a_guess(self) -> None:
        assert guess_category("Some Local Bakery") is None


class TestParsing:
    def test_a_plain_signed_amount_export(self) -> None:
        csv_text = (
            "Date,Description,Amount\n"
            "2026-01-05,SQ *ANYTIME FITNESS 4471,49.00\n"
            "2026-02-05,SQ *ANYTIME FITNESS 4471,49.00\n"
            "2026-03-05,SQ *ANYTIME FITNESS 4471,49.00\n"
        )
        charges = parse_csv(csv_text)
        assert len(charges) == 3
        assert {c.vendor for c in charges} == {"Anytime Fitness"}
        assert charges[0].category == "gym"
        assert charges[0].date == dt.date(2026, 1, 5)
        assert all(not c.is_credit for c in charges)

    def test_purchases_exported_as_negative_are_flipped(self) -> None:
        """The silent inversion: get this wrong and every refund becomes a charge."""
        csv_text = (
            "Date,Description,Amount\n"
            "01/05/2026,ANYTIME FITNESS,-49.00\n"
            "02/05/2026,ANYTIME FITNESS,-49.00\n"
            "02/09/2026,WAYFAIR REFUND,88.00\n"
        )
        charges = parse_csv(csv_text)
        gym = [c for c in charges if c.vendor == "Anytime Fitness"]
        refund = [c for c in charges if "Wayfair" in c.vendor]
        assert all(not c.is_credit for c in gym), "purchases must read as debits"
        assert refund and refund[0].is_credit, "the refund must read as a credit"

    def test_separate_debit_and_credit_columns(self) -> None:
        csv_text = (
            "Posted Date,Details,Debit,Credit\n"
            "03/01/2026,COMCAST,89.99,\n"
            "03/09/2026,COMCAST,,20.00\n"
        )
        charges = parse_csv(csv_text)
        assert len(charges) == 2
        assert not charges[0].is_credit
        assert charges[1].is_credit

    def test_parenthesised_and_trailing_minus_credits(self) -> None:
        csv_text = (
            "Date,Description,Amount\n"
            "2026-03-01,COMCAST,89.99\n"
            "2026-03-02,COMCAST,89.99\n"
            "2026-03-09,COMCAST,(20.00)\n"
        )
        charges = parse_csv(csv_text)
        assert [c.is_credit for c in charges] == [False, False, True]

    def test_currency_symbols_and_thousands_separators(self) -> None:
        csv_text = 'Date,Description,Amount\n2026-03-01,DELTA,"$1,412.30"\n'
        charges = parse_csv(csv_text)
        assert charges[0].amount_usd == 1412.30

    def test_unparseable_rows_are_skipped_not_guessed(self) -> None:
        csv_text = (
            "Date,Description,Amount\n"
            "2026-03-01,COMCAST,89.99\n"
            "not a date,COMCAST,89.99\n"
            "2026-03-03,,50.00\n"
            "2026-03-04,COMCAST,not a number\n"
            "2026-03-05,COMCAST,0.00\n"
        )
        charges = parse_csv(csv_text)
        assert len(charges) == 1

    def test_empty_and_headerless_input(self) -> None:
        assert parse_csv("") == []
        assert parse_csv("just,some,columns\n1,2,3\n") == []

    def test_short_rows_do_not_raise(self) -> None:
        csv_text = "Date,Description,Amount\n2026-03-01,COMCAST\n2026-03-02,COMCAST,89.99\n"
        assert len(parse_csv(csv_text)) == 1

    def test_source_ids_point_back_at_the_line(self) -> None:
        csv_text = "Date,Description,Amount\n2026-03-01,COMCAST,89.99\n"
        assert parse_csv(csv_text, source_prefix="chase")[0].source_id == "chase:2"


class TestEndToEnd:
    def test_a_statement_becomes_a_ranked_leak(self) -> None:
        """The path a judge will actually watch: file in, dollars out."""
        from dial.leaks import LeakKind, Route, detect

        rows = ["Date,Description,Amount"]
        for month in range(1, 5):
            rows.append(f"2026-0{month}-05,SQ *ANYTIME FITNESS 4471,49.00")
        for month in range(5, 9):
            rows.append(f"2026-0{month}-05,ANYTIME FITNESS INC,69.00")

        charges = parse_csv("\n".join(rows))
        leaks = detect(charges, [], today=dt.date(2026, 9, 1))

        rises = [leak for leak in leaks if leak.kind is LeakKind.SILENT_PRICE_RISE]
        assert len(rises) == 1, "descriptor variants must not hide the cadence"
        assert rises[0].vendor == "Anytime Fitness"
        assert rises[0].annual_usd == 240.0
        assert rises[0].route is Route.PHONE_REQUIRED

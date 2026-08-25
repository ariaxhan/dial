"""The legal boundary is the thing most worth testing, so it is tested first.

Every case here is a number Dial either may or may not place an AI-voice call to. A
regression in this file is a statutory violation, not a style problem.
"""

from __future__ import annotations

import pytest

from dial.lines import (
    LineKind,
    RefusedNumber,
    assert_callable,
    classify,
)


class TestCallable:
    @pytest.mark.parametrize(
        "number",
        [
            "1-800-555-0199",
            "(833) 555-0142",
            "+1 844 555 0100",
            "855-555-0111",
            "866 555 0123",
            "877-555-0155",
            "888-555-0177",
        ],
    )
    def test_toll_free_is_callable(self, number: str) -> None:
        verdict = classify(number)
        assert verdict.kind is LineKind.TOLL_FREE
        assert verdict.callable
        assert verdict.e164 is not None and verdict.e164.startswith("+1")

    def test_assert_callable_returns_e164(self) -> None:
        assert assert_callable("1 (800) 555-0199") == "+18005550199"


class TestRefused:
    def test_wireless_is_refused(self) -> None:
        # A US mobile number. 227(b)(1)(A)(iii) territory.
        verdict = classify("+1 619 555 0123")
        assert not verdict.callable

    def test_unparseable_is_refused(self) -> None:
        verdict = classify("call me maybe")
        assert verdict.kind is LineKind.UNPARSEABLE
        assert not verdict.callable

    def test_invalid_number_is_refused(self) -> None:
        assert not classify("+1 000 000 0000").callable

    def test_empty_is_refused(self) -> None:
        assert not classify("").callable

    def test_assert_callable_raises_with_the_reason(self) -> None:
        with pytest.raises(RefusedNumber) as excinfo:
            assert_callable("not a number")
        assert excinfo.value.verdict.callable is False
        assert excinfo.value.verdict.reason

    def test_uk_mobile_is_refused(self) -> None:
        assert not classify("+44 7911 123456", region="GB").callable


class TestUnknownIsUnsafe:
    """The default has to be refusal, or the boundary leaks."""

    def test_a_plain_landline_is_not_assumed_to_be_a_business(self) -> None:
        # Fixed line, but nothing establishes it as a business line, so it is refused
        # rather than dialed on an assumption.
        verdict = classify("+1 212 555 0100")
        assert not verdict.callable
        assert verdict.kind in (
            LineKind.RESIDENTIAL_OR_UNKNOWN_LANDLINE,
            LineKind.WIRELESS,
            LineKind.VOIP,
        )

"""The offline half of the preflight. The live half is proven by running it against AWS.

Recorded evidence, us-west-2, 2026-08-24:

    amazon.nova-2-sonic-v1:0              ok=True  after 2.08s, 3 chunks, 10,204 bytes
    amazon.nova-2-sonic-DOES-NOT-EXIST:0  ok=False after 45.0s, 0 chunks

The negative case is the point. A bad model id produces silence rather than an error, so a
check that cannot fail on it is not a check.
"""

from __future__ import annotations

import wave

import pytest

from dial.preflight import (
    INPUT_CHANNELS,
    INPUT_RATE,
    INPUT_WIDTH,
    PROBE_PATH,
    PreflightResult,
    read_pcm,
)


class TestTheProbe:
    def test_the_probe_ships_with_the_repo(self) -> None:
        assert PROBE_PATH.exists(), "the preflight cannot run without its probe audio"

    def test_the_probe_matches_nova_sonics_input_contract(self) -> None:
        with wave.open(str(PROBE_PATH)) as w:
            assert w.getframerate() == INPUT_RATE
            assert w.getnchannels() == INPUT_CHANNELS
            assert w.getsampwidth() == INPUT_WIDTH

    def test_the_probe_is_short_enough_to_gate_a_call_on(self) -> None:
        with wave.open(str(PROBE_PATH)) as w:
            seconds = w.getnframes() / w.getframerate()
        assert seconds < 4, "a preflight nobody wants to wait for is a preflight nobody runs"


class TestReadPcm:
    def test_trailing_silence_is_appended(self) -> None:
        """Without it the model waits for the speaker and a good probe times out."""
        bare = read_pcm(PROBE_PATH, trailing_silence_s=0)
        padded = read_pcm(PROBE_PATH, trailing_silence_s=1.0)
        assert len(padded) - len(bare) == INPUT_RATE * INPUT_WIDTH
        assert padded.endswith(b"\x00" * 64)

    def test_a_wrong_format_probe_is_refused(self, tmp_path) -> None:
        bad = tmp_path / "bad.wav"
        with wave.open(str(bad), "wb") as w:
            w.setnchannels(2)          # stereo, 44.1k: the usual export defaults
            w.setsampwidth(2)
            w.setframerate(44_100)
            w.writeframes(b"\x00" * 400)
        with pytest.raises(ValueError, match="16000 Hz mono"):
            read_pcm(bad)

    def test_a_missing_probe_raises_rather_than_returning_silence(self, tmp_path) -> None:
        with pytest.raises(OSError):
            read_pcm(tmp_path / "nope.wav")


class TestResult:
    def test_a_result_is_truthy_only_when_it_passed(self) -> None:
        assert PreflightResult(True, "fine")
        assert not PreflightResult(False, "nope")

    def test_a_failure_always_carries_a_reason(self) -> None:
        assert PreflightResult(False, "only 0 audio chunks came back").reason

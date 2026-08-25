"""Prove the voice stack works before a single number is dialed.

This module exists because of one measured fact. `BidiAgent.start()` reports success for a
model id that does not exist. It never contacts AWS, so "the session opened" is not evidence
of anything, and the symptom of a misconfigured Nova Sonic is silence on a live call rather
than an exception.

Measured on 2026-08-24, us-west-2:

    amazon.nova-2-sonic-v1:0              start() -> STARTED, audio round trip -> 25 chunks
    amazon.nova-2-sonic-DOES-NOT-EXIST:0  start() -> STARTED, audio round trip -> nothing

Only the second column discriminates. So the preflight is a real audio round trip against a
short speech probe, on a timeout, and a call is not placed unless it passes.
"""

from __future__ import annotations

import asyncio
import base64
import time
import wave
from dataclasses import dataclass
from pathlib import Path

__all__ = ["PreflightResult", "PROBE_PATH", "check_voice", "read_pcm"]

PROBE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "preflight-probe.wav"

#: Nova Sonic's input contract: 16 kHz, mono, signed 16-bit little-endian PCM.
INPUT_RATE = 16_000
INPUT_CHANNELS = 1
INPUT_WIDTH = 2

_CHUNK_SAMPLES = 1024


@dataclass(frozen=True)
class PreflightResult:
    """Whether the voice stack is actually working, and the evidence either way."""

    ok: bool
    reason: str
    audio_chunks: int = 0
    audio_bytes: int = 0
    heard: tuple[str, ...] = ()
    seconds: float = 0.0

    def __bool__(self) -> bool:
        return self.ok


def read_pcm(path: Path | str = PROBE_PATH, *, trailing_silence_s: float = 1.0) -> bytes:
    """Read a probe WAV as raw PCM, with trailing silence so turn detection fires.

    Without the silence the model waits for the speaker to finish and the round trip times
    out on a probe that was actually fine.
    """
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (
            INPUT_RATE,
            INPUT_CHANNELS,
            INPUT_WIDTH,
        ):
            raise ValueError(
                f"probe must be {INPUT_RATE} Hz mono 16-bit, got "
                f"{w.getframerate()} Hz, {w.getnchannels()} channel(s), "
                f"{w.getsampwidth() * 8}-bit"
            )
        data = w.readframes(w.getnframes())
    return data + b"\x00" * int(INPUT_RATE * INPUT_WIDTH * trailing_silence_s)


async def check_voice(
    *,
    model_id: str = "amazon.nova-2-sonic-v1:0",
    region: str = "us-west-2",
    probe: Path | str = PROBE_PATH,
    timeout_s: float = 45.0,
    required_chunks: int = 3,
) -> PreflightResult:
    """Send real speech to Nova Sonic and require real audio back.

    Returns rather than raises, so a caller can degrade gracefully instead of dying, and
    every failure carries the reason it failed.
    """
    started = time.monotonic()

    try:
        from strands.experimental.bidi import BidiAgent
        from strands.experimental.bidi.models import BidiNovaSonicModel
        from strands.experimental.bidi.types.events import BidiAudioInputEvent
    except ImportError as exc:
        return PreflightResult(
            False,
            # The bidi extra is version-fragile: strands declares a range wider than its
            # own imports support. See pyproject for the pin.
            f"strands bidi is not importable ({exc}). Check the "
            f"aws-sdk-bedrock-runtime pin.",
        )

    try:
        pcm = read_pcm(probe)
    except (OSError, ValueError) as exc:
        return PreflightResult(False, f"probe audio unusable: {exc}")

    model = BidiNovaSonicModel(
        model_id=model_id,
        provider_config={"audio": {"voice": "tiffany"}},
        client_config={"region": region},
    )
    agent = BidiAgent(model=model, system_prompt="Reply with one very short sentence.")

    chunks = 0
    total_bytes = 0
    heard: list[str] = []
    enough = asyncio.Event()

    async def receive() -> None:
        nonlocal chunks, total_bytes
        async for event in agent.receive():
            payload = event if isinstance(event, dict) else {}
            audio = payload.get("audio")
            if audio:
                chunks += 1
                total_bytes += len(audio)
            text = payload.get("text")
            if text and str(text).strip():
                heard.append(str(text).strip())
            if chunks >= required_chunks:
                enough.set()
                return

    receiver: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(agent.start(), timeout=timeout_s)
        receiver = asyncio.create_task(receive())

        step = _CHUNK_SAMPLES * INPUT_WIDTH
        for offset in range(0, len(pcm), step):
            await agent.send(
                BidiAudioInputEvent(
                    audio=base64.b64encode(pcm[offset : offset + step]).decode("ascii"),
                    format="pcm",
                    sample_rate=INPUT_RATE,
                    channels=INPUT_CHANNELS,
                )
            )
            await asyncio.sleep(0.008)

        remaining = max(1.0, timeout_s - (time.monotonic() - started))
        try:
            await asyncio.wait_for(enough.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            pass
    except asyncio.TimeoutError:
        return PreflightResult(
            False,
            f"timed out after {timeout_s:.0f}s. Nova Sonic hangs rather than raising on a "
            f"bad model id, region, or credential.",
            chunks,
            total_bytes,
            tuple(heard),
            round(time.monotonic() - started, 2),
        )
    except Exception as exc:  # noqa: BLE001 - a preflight reports, it does not propagate
        return PreflightResult(
            False,
            f"{type(exc).__name__}: {exc}",
            chunks,
            total_bytes,
            tuple(heard),
            round(time.monotonic() - started, 2),
        )
    finally:
        if receiver is not None:
            receiver.cancel()
        try:
            await asyncio.wait_for(agent.stop(), timeout=15)
        except Exception:  # noqa: BLE001 - already reporting a result
            pass

    elapsed = round(time.monotonic() - started, 2)
    if chunks < required_chunks:
        return PreflightResult(
            False,
            f"only {chunks} audio chunks came back, needed {required_chunks}. "
            f"This is what a wrong model id looks like: silence, not an error.",
            chunks,
            total_bytes,
            tuple(heard),
            elapsed,
        )

    return PreflightResult(
        True,
        f"{model_id} in {region} answered with {total_bytes:,} bytes of audio",
        chunks,
        total_bytes,
        tuple(heard),
        elapsed,
    )

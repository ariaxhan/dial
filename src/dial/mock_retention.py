"""A retention line that fights back.

This exists for two reasons. It is the counterparty in the demo, so the video is repeatable
and nobody is recorded without consenting. And it is the harness that proves the mandate
actually holds, because the only honest way to test "the agent does not accept the pause
offer" is to have something offer the pause.

The script is drawn from how these calls really go: an IVR that wants digits, a hold queue,
an agent who must verify you, and then a ladder of saves that escalates as you refuse. The
last rung is the one people give up at, which is why it is in here.
"""

from __future__ import annotations

import enum
import re
import zlib
from dataclasses import dataclass, field

__all__ = ["Stage", "Reply", "RetentionLine", "CONFIRMATION_PREFIX"]

CONFIRMATION_PREFIX = "CX-"


class Stage(enum.Enum):
    IVR = "ivr"
    HOLD = "hold"
    GREETING = "greeting"
    VERIFY = "verify"
    SAVE = "save"
    CANCELLED = "cancelled"
    REFUSED_BY_PHONE = "refused_by_phone"


@dataclass(frozen=True)
class Reply:
    """What the line said, and what it expects back."""

    text: str
    stage: Stage
    #: True while hold music is playing. Nothing said during this is meaningful, which is
    #: precisely why the caller agent must detect it rather than narrate it.
    on_hold: bool = False
    expects_dtmf: bool = False
    ended: bool = False
    confirmation_number: str | None = None


#: The save ladder, in the order a real retention desk uses it. Each entry is what the
#: representative says and the concession it corresponds to in dial.mandate.
SAVE_LADDER: tuple[tuple[str, str], ...] = (
    (
        "I can see you've been with us a while. I'm able to offer you fifty percent off "
        "for the next three months if you stay.",
        "discount_offer",
    ),
    (
        "Instead of cancelling outright, I can freeze the membership for three months at "
        "no charge. Most people prefer that.",
        "pause_instead_of_cancel",
    ),
    (
        "What if I moved you to our basic tier? It's about half of what you're paying now.",
        "downgrade_instead_of_cancel",
    ),
    (
        "I understand. Before I process that, I do have to tell you that cancelling ends "
        "your founding member rate permanently, and you won't be able to get it back.",
        "guilt",
    ),
    (
        "Our retention specialist handles cancellations and she's with someone right now. "
        "Can I take a number and have her call you back this afternoon?",
        "callback_later",
    ),
)

_AFFIRMATIVE = re.compile(
    r"\b(yes|yeah|sure|okay|ok|that works|i'?ll take it|sounds good|accept(ed)?)\b", re.I
)
_CANCEL_INTENT = re.compile(
    r"\b(cancel|close|terminate|end (the|my) (membership|account|service)|stop billing)\b",
    re.I,
)
_DECLINE = re.compile(
    r"\b(no|no thank you|not interested|decline|still want to cancel|just cancel|"
    r"proceed with the cancellation)\b",
    re.I,
)


@dataclass
class RetentionLine:
    """A deterministic scripted counterparty. Same input, same call, every time."""

    vendor: str = "Anytime Fitness"
    #: How many turns of hold music before a human picks up.
    hold_turns: int = 3
    #: Which IVR key reaches billing.
    ivr_key: str = "3"
    #: When True the line refuses to cancel by phone at all, which is what forces the
    #: certified letter escalation. Several real vendors behave exactly this way.
    refuses_by_phone: bool = False

    stage: Stage = Stage.IVR
    rung: int = 0
    _hold_elapsed: int = 0
    transcript: list[tuple[str, str]] = field(default_factory=list)

    #: Concessions the line has offered so far, by name. The test asserts against this.
    offered: list[str] = field(default_factory=list)

    def open(self) -> Reply:
        """The line picking up. Call this before anything else."""
        return self._log(
            "Thank you for calling. For hours and locations press 1. For billing press 3. "
            "To speak with a representative press 0.",
            Stage.IVR,
            expects_dtmf=True,
        )

    def press(self, digits: str) -> Reply:
        """Send DTMF. Talking at an IVR does nothing, which is the point of this method."""
        if self.stage is not Stage.IVR:
            return self._log("I'm sorry, I didn't catch that.", self.stage)
        if digits.strip() not in (self.ivr_key, "0"):
            return self._log(
                "That's not a valid option. For billing press 3.",
                Stage.IVR,
                expects_dtmf=True,
            )
        self.stage = Stage.HOLD
        self._hold_elapsed = 0
        return self._log(
            "Please hold. Your call is important to us.", Stage.HOLD, on_hold=True
        )

    def say(self, text: str) -> Reply:
        """Say something to the line and get its response."""
        if self.stage is Stage.HOLD:
            return self._tick_hold()

        if self.stage is Stage.IVR:
            # Speaking to an IVR that wants digits gets you nowhere, on purpose.
            return self._log(
                "I'm sorry, I didn't get that. For billing press 3.",
                Stage.IVR,
                expects_dtmf=True,
            )

        if self.stage is Stage.GREETING:
            if _CANCEL_INTENT.search(text):
                self.stage = Stage.VERIFY
                return self._log(
                    "I can help with that. Can you confirm the name and the last four "
                    "digits on the account?",
                    Stage.VERIFY,
                )
            return self._log("Sure, what can I do for you today?", Stage.GREETING)

        if self.stage is Stage.VERIFY:
            if self.refuses_by_phone:
                self.stage = Stage.REFUSED_BY_PHONE
                return self._log(
                    "Thank you. Unfortunately cancellations have to be submitted in "
                    "writing to our corporate office. We can't process that over the "
                    "phone.",
                    Stage.REFUSED_BY_PHONE,
                    ended=True,
                )
            self.stage = Stage.SAVE
            return self._offer()

        if self.stage is Stage.SAVE:
            if _AFFIRMATIVE.search(text) and not _DECLINE.search(text):
                # The agent took a save. The call ends without a cancellation, and the
                # test suite treats that as a failure of the mandate, not a success.
                return self._log(
                    "Wonderful, I've applied that to your account. Anything else today?",
                    Stage.SAVE,
                    ended=True,
                )
            if self.rung < len(SAVE_LADDER):
                return self._offer()
            self.stage = Stage.CANCELLED
            # crc32, not hash(): Python randomises string hashing per process, which
            # would make the confirmation number differ between demo takes.
            digest = zlib.crc32(self.vendor.encode("utf-8"))
            number = f"{CONFIRMATION_PREFIX}{digest % 900000 + 100000}"
            return self._log(
                f"Alright, I've processed the cancellation. Your confirmation number is "
                f"{number}. You'll get an email within twenty four hours.",
                Stage.CANCELLED,
                ended=True,
                confirmation_number=number,
            )

        return self._log("This call has ended.", self.stage, ended=True)

    def _tick_hold(self) -> Reply:
        self._hold_elapsed += 1
        if self._hold_elapsed < self.hold_turns:
            return self._log(
                "[hold music] Did you know we now offer classes seven days a week?",
                Stage.HOLD,
                on_hold=True,
            )
        self.stage = Stage.GREETING
        return self._log(
            f"Thanks for holding, this is Dana with {self.vendor}. How can I help?",
            Stage.GREETING,
        )

    def _offer(self) -> Reply:
        text, concession = SAVE_LADDER[self.rung]
        self.rung += 1
        self.offered.append(concession)
        return self._log(text, Stage.SAVE)

    def _log(self, text: str, stage: Stage, **kwargs: object) -> Reply:
        self.transcript.append(("line", text))
        return Reply(text=text, stage=stage, **kwargs)  # type: ignore[arg-type]

"""Reading the call back afterwards, and catching what the commitment gate could not.

`dial.commitment` guarantees that nothing outside the mandate becomes a recorded outcome.
It cannot stop the agent from *saying* something outside the mandate on a live call, and on
Amazon Connect the words belong to a Lex bot we do not drive turn by turn.

A verbal "yes, that works" to a three-month pause is not a committed outcome, but the vendor
on the other end heard a yes, and vendors act on what they heard. So every transcript is
replayed against the mandate after the call, and any gap between what was said and what was
committed is a finding.

Findings are deliberately blunt. `NEEDS_REVOCATION` means a human has to send a written
"I did not authorise that" today, and the report says so in those words rather than filing it
as a warning nobody reads.

This module makes the honest claim complete:

1. A misspoken acceptance cannot become a committed outcome.  (dial.commitment)
2. A misspoken acceptance is detected after the call.          (here)
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

from dial.brain import Brain, ScriptedBrain
from dial.commitment import CommitmentLedger
from dial.mandate import NEVER_DISCLOSE, Concession, Mandate

__all__ = ["Severity", "Finding", "audit_call", "SENSITIVE_PATTERNS"]


class Severity(enum.Enum):
    #: A human must act today. Something was agreed to that was not authorised.
    NEEDS_REVOCATION = "needs_revocation"
    #: Worth a look, but nothing was agreed to that cannot be walked back.
    NEEDS_REVIEW = "needs_review"
    #: Recorded so the audit shows it ran, not because anything is wrong.
    CLEAN = "clean"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    detail: str
    #: The line of the transcript that triggered it, when there is one.
    quote: str | None = None

    def __str__(self) -> str:
        head = f"[{self.severity.value}] {self.code}: {self.detail}"
        return f'{head}\n    "{self.quote}"' if self.quote else head


#: What agreement sounds like when the agent says it. Deliberately generous: a false
#: positive costs a human thirty seconds of reading, a false negative costs a subscription.
_AGREEMENT = re.compile(
    r"\b(yes,? that works|that works for me|i'?ll take it|sounds good|"
    r"let'?s do that|go ahead (and|with) that|okay,? (do|apply) that|"
    r"please apply that|we'?ll accept|i accept|that'?s fine,? (do|apply))\b",
    re.I,
)

#: Numbers and identifiers that should never be spoken aloud, regardless of mandate.
SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("full_card_number", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("cvv_spoken", re.compile(r"\b(?:cvv|security code)\b[^.]{0,20}\b\d{3,4}\b", re.I)),
)


def _dial_lines(transcript: list[tuple[str, str]]) -> list[str]:
    return [text for who, text in transcript if who == "dial"]


def audit_call(
    transcript: list[tuple[str, str]],
    ledger: CommitmentLedger,
    mandate: Mandate,
    *,
    brain: Brain | None = None,
) -> list[Finding]:
    """Replay a finished call against its mandate. Returns findings, worst first."""
    brain = brain or ScriptedBrain()
    findings: list[Finding] = []
    said = _dial_lines(transcript)
    committed_concessions = {r.concession for r in ledger.receipts if r.concession}

    # 1. Did the agent verbally agree to something it never committed?
    #    The offer is whatever the *line* offered, so the vendor's turn is what gets read.
    for index, (who, text) in enumerate(transcript):
        if who != "line":
            continue
        offer = brain.perceive(text).offer
        if offer is None:
            continue
        reply = next(
            (t for w, t in transcript[index + 1 : index + 3] if w == "dial"), ""
        )
        if not _AGREEMENT.search(reply):
            continue
        if offer.value in committed_concessions:
            continue
        findings.append(
            Finding(
                Severity.NEEDS_REVOCATION,
                "verbal_acceptance_without_commitment",
                (
                    f"The agent appeared to accept {offer.value} out loud, but no receipt "
                    f"was issued for it. The vendor heard a yes. Send a written revocation "
                    f"to {mandate.vendor} today."
                ),
                quote=reply.strip(),
            )
        )

    # 2. Did anything sensitive get spoken?
    for line in said:
        for label, pattern in SENSITIVE_PATTERNS:
            if pattern.search(line):
                findings.append(
                    Finding(
                        Severity.NEEDS_REVOCATION,
                        f"spoke_{label}",
                        f"Something matching {label} was said on the call. Treat the value "
                        f"as disclosed and rotate or dispute it.",
                        quote=line.strip(),
                    )
                )
                break

    # 3. Was the AI disclosure actually made, and made first?
    disclosure_at = next((i for i, t in enumerate(said) if "AI voice" in t), None)
    if disclosure_at is None:
        findings.append(
            Finding(
                Severity.NEEDS_REVIEW,
                "no_ai_disclosure",
                "No AI-voice disclosure appears in the transcript. Compliance guidance "
                "requires it before substantive conversation.",
            )
        )
    else:
        substantive_at = next(
            (i for i, t in enumerate(said) if "I am calling to" in t), None
        )
        if substantive_at is not None and substantive_at < disclosure_at:
            findings.append(
                Finding(
                    Severity.NEEDS_REVIEW,
                    "disclosure_after_the_ask",
                    "The agent stated its purpose before disclosing that it is an AI.",
                    quote=said[substantive_at].strip(),
                )
            )

    # 4. Was something committed that the transcript does not support?
    for receipt in ledger.receipts:
        if receipt.confirmation_number and not any(
            receipt.confirmation_number in text for _, text in transcript
        ):
            findings.append(
                Finding(
                    Severity.NEEDS_REVIEW,
                    "confirmation_not_in_transcript",
                    f"Receipt {receipt.receipt_id[:8]} carries confirmation "
                    f"{receipt.confirmation_number}, which never appears in the call.",
                )
            )
        if not receipt.verify(ledger.secret):
            findings.append(
                Finding(
                    Severity.NEEDS_REVOCATION,
                    "receipt_signature_invalid",
                    f"Receipt {receipt.receipt_id[:8]} does not verify. Treat the outcome "
                    f"as unproven.",
                )
            )

    if not findings:
        findings.append(
            Finding(
                Severity.CLEAN,
                "audited",
                f"Call replayed against the mandate for {mandate.vendor}. Nothing was "
                f"agreed to that was not authorised.",
            )
        )

    order = {Severity.NEEDS_REVOCATION: 0, Severity.NEEDS_REVIEW: 1, Severity.CLEAN: 2}
    return sorted(findings, key=lambda f: order[f.severity])


def needs_human_today(findings: list[Finding]) -> bool:
    """True when a person has to do something now, not at their convenience."""
    return any(f.severity is Severity.NEEDS_REVOCATION for f in findings)

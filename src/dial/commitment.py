"""What the agent may *commit*, when it no longer owns the conversation loop.

Dial's thesis is that authority lives in code the model cannot reach. That was easy to
guarantee while `dial.caller` drove the call: the refusal sentence was generated *from* the
mandate ruling, so there was no path around it.

On Amazon Connect the loop belongs to AWS. A Lex bot with a Nova Sonic voice runs the
conversation and reaches our logic through tool calls. A tool is something the model chooses
to invoke, and a model can choose not to. Taken naively that turns the mandate from a
structural guarantee into a strongly-worded suggestion, which is the whole ballgame.

The fix is to stop trying to control what the agent *says* and control what it can *commit*.

    Talking is free. Committing is gated.

The agent may say anything on the call. But a cancellation is only real when it produces a
receipt, and only `commit()` issues receipts, and `commit()` consults the mandate. An agent
that skips the tool ends the call having achieved nothing recordable. An agent that verbally
accepts a three-month pause it was not authorized to accept produces no receipt for it, and
`dial.audit` catches the discrepancy afterwards and escalates.

So the honest claim is not "the model cannot misspeak". It is:

1. A misspoken acceptance cannot become a committed outcome.
2. A misspoken acceptance is detected after the call, not discovered on a bill.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass, field

from dial.caller import Outcome
from dial.mandate import Concession, Mandate, MandateViolation, evaluate_offer

__all__ = [
    "Receipt",
    "CommitmentRefused",
    "CommitmentLedger",
    "COMMITTABLE_OUTCOMES",
]

#: Outcomes that represent something actually happening to the account. Everything else is
#: a description of how a call went and needs no receipt.
COMMITTABLE_OUTCOMES = frozenset(
    {
        Outcome.OBJECTIVE_MET,
        Outcome.CONCESSION_ACCEPTED,
    }
)

_SECRET_ENV = "DIAL_RECEIPT_SECRET"


class CommitmentRefused(RuntimeError):
    """The agent tried to commit something the mandate does not permit."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Receipt:
    """Proof that an outcome was permitted. Without one, nothing happened.

    The signature exists so a receipt cannot be fabricated by anything downstream that only
    has the text of a call. It is not protecting against a malicious model; it is making the
    boundary explicit enough that a future refactor cannot accidentally route around it.
    """

    receipt_id: str
    vendor: str
    principal: str
    outcome: str
    concession: str | None
    confirmation_number: str | None
    issued_at: str
    signature: str

    def verify(self, secret: str | None = None) -> bool:
        return hmac.compare_digest(self.signature, _sign(self.unsigned(), secret))

    def unsigned(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "vendor": self.vendor,
            "principal": self.principal,
            "outcome": self.outcome,
            "concession": self.concession,
            "confirmation_number": self.confirmation_number,
            "issued_at": self.issued_at,
        }


def _secret(explicit: str | None = None) -> str:
    # A development default is fine here: the signature is an integrity boundary inside one
    # process, not a defence against an attacker who already runs our code.
    return explicit or os.environ.get(_SECRET_ENV) or "dial-development-secret"


def _sign(payload: dict[str, object], secret: str | None = None) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(_secret(secret).encode("utf-8"), body, hashlib.sha256).hexdigest()


@dataclass
class CommitmentLedger:
    """The only thing that can turn a conversation into an outcome.

    One ledger per call. It is deliberately not a general service: a mandate authorises one
    objective against one vendor, and a ledger that could commit against two vendors would
    be a way to launder authority from one call into another.
    """

    mandate: Mandate
    now: dt.datetime
    secret: str | None = None
    receipts: list[Receipt] = field(default_factory=list)
    #: Everything the agent tried to commit and was refused, kept for the audit.
    refusals: list[tuple[str, str]] = field(default_factory=list)

    def commit(
        self,
        outcome: Outcome,
        *,
        concession: Concession | None = None,
        confirmation_number: str | None = None,
        amount_usd: float = 0.0,
        costs_usd: float = 0.0,
    ) -> Receipt:
        """Record an outcome, or refuse. This is the only door.

        Raises `CommitmentRefused` rather than returning a failure object, because a caller
        that ignores a returned error still ends up with an outcome it should not have.
        """
        if outcome not in COMMITTABLE_OUTCOMES:
            raise CommitmentRefused(
                f"{outcome.value} is not an outcome that changes the account; "
                f"nothing to commit"
            )

        try:
            self.mandate.assert_live(self.now)
        except MandateViolation as exc:
            self.refusals.append((outcome.value, str(exc)))
            raise CommitmentRefused(str(exc)) from exc

        if outcome is Outcome.OBJECTIVE_MET:
            if not confirmation_number:
                # A cancellation nobody can prove is a cancellation that did not happen.
                # Vendors reverse these, and "the agent said it was done" is not evidence.
                reason = "objective_met requires a confirmation number"
                self.refusals.append((outcome.value, reason))
                raise CommitmentRefused(reason)

        if outcome is Outcome.CONCESSION_ACCEPTED:
            if concession is None:
                reason = "concession_accepted requires which concession was accepted"
                self.refusals.append((outcome.value, reason))
                raise CommitmentRefused(reason)
            ruling = evaluate_offer(
                self.mandate,
                concession,
                now=self.now,
                amount_usd=amount_usd,
                costs_usd=costs_usd,
            )
            if not ruling.accept:
                self.refusals.append((concession.value, ruling.reason))
                raise CommitmentRefused(
                    f"cannot commit {concession.value}: {ruling.reason}"
                )

        payload = {
            "receipt_id": uuid.uuid4().hex,
            "vendor": self.mandate.vendor,
            "principal": self.mandate.principal,
            "outcome": outcome.value,
            "concession": concession.value if concession else None,
            "confirmation_number": confirmation_number,
            "issued_at": self.now.isoformat(),
        }
        receipt = Receipt(**payload, signature=_sign(payload, self.secret))  # type: ignore[arg-type]
        self.receipts.append(receipt)
        return receipt

    @property
    def committed_anything(self) -> bool:
        return bool(self.receipts)

    def outcome_of_record(self) -> Receipt | None:
        """The single receipt that describes what happened, or None.

        None is a real answer and the common one: most calls end without changing anything,
        and a product that reports success in that case is lying.
        """
        return self.receipts[-1] if self.receipts else None

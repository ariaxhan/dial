# AWS resources created for Dial

Account **114829893009**, region **us-west-2**, created 2026-08-25.

us-west-2 was chosen because it is the only region that has both Nova Sonic and Amazon Q in
Connect, and Bedrock access there is already verified. Anything cross-region would add
transfer cost and latency to a real-time voice loop.

Everything is prefixed `dial-` so it can be found and removed in one pass.

## What exists

| Resource | Identifier | Recurring cost |
| --- | --- | --- |
| Connect instance | `dial-agent` <br> `3672168c-d072-4715-beda-babeeaa6b10f` | none for the instance itself |
| Toll-free number | **+1 877 523 5531** <br> `b5fe0ed4-ca41-412d-9fcd-23e703e418d5` | about $1.80/month, plus per minute |
| Q in Connect assistant | `dial-assistant` <br> `6ff8e798-0e4a-4dc4-aea0-5d8bb52d8eac` | none idle |
| Integration association | `70029a56-e25e-4b07-aed8-9292b31c30f2` | none |

Full ARNs:

```
arn:aws:connect:us-west-2:114829893009:instance/3672168c-d072-4715-beda-babeeaa6b10f
arn:aws:connect:us-west-2:114829893009:phone-number/b5fe0ed4-ca41-412d-9fcd-23e703e418d5
arn:aws:wisdom:us-west-2:114829893009:assistant/6ff8e798-0e4a-4dc4-aea0-5d8bb52d8eac
```

## Cost, honestly

The phone number is the only thing billing while nothing is happening: roughly **$1.80 a
month**. Calls add about **$0.0048/minute** for outbound voice and **$0.02/minute** for Nova
Sonic inference, so a five minute call is about **twelve cents**.

The account-wide guardrail applies on top: a $3/day email alert, and from 2026-09-01 a
$40/month budget that automatically attaches a deny policy which includes
`connect:StartOutboundVoiceContact` and `connect:ClaimPhoneNumber`. A runaway dialer stops
itself. See `Vaults/_meta/aws-guardrail/README.md`.

## Tearing it down

In this order, because the number must be released before the instance goes.

```bash
export AWS_PROFILE=keystone
R="--region us-west-2"

aws connect release-phone-number --phone-number-id b5fe0ed4-ca41-412d-9fcd-23e703e418d5 $R
aws connect delete-integration-association \
  --instance-id 3672168c-d072-4715-beda-babeeaa6b10f \
  --integration-association-id 70029a56-e25e-4b07-aed8-9292b31c30f2 $R
aws qconnect delete-assistant --assistant-id 6ff8e798-0e4a-4dc4-aea0-5d8bb52d8eac $R
aws connect delete-instance --instance-id 3672168c-d072-4715-beda-babeeaa6b10f $R
```

Releasing a toll-free number is **not instantly reversible**: the number goes back to the pool
and is unlikely to be reclaimable. The kernel bash guard blocks destructive AWS calls and will
require a human approval token for each of these, which is correct.

## What is still missing

The infrastructure is up; the agent behaviour is not configured yet.

1. **Conversational AI bot with Nova Sonic.** The assistant exists and carries AWS's built-in
   system agents, including `SelfServiceOrchestratorVoice` of type `ORCHESTRATION`, which is
   the voice self-service orchestrator. Dial needs a customised agent on top of it, with
   `AMAZON.QinConnectIntent` bridging the deterministic Lex layer to the generative one.
   Without that intent the bot behaves as a plain IVR and never hands off.
2. **Contact flow.** Needs a `Set voice` block using a Nova Sonic voice (Matthew, Amy, Olivia
   or Lupe) with speaking style **Generative**, and a `Get customer input` block with
   **Enable AI Agent** switched on.
3. **Tools.** The mandate has to reach the agent as tools. `dial/commitment.py` is the surface
   that matters: the agent may say anything, but only `CommitmentLedger.commit()` issues a
   receipt and it consults the mandate.
4. **Outbound.** `aws connect start-outbound-voice-contact` with the flow id, the instance id,
   `+18775235531` as the source, and the vendor's number as the destination, which must pass
   `dial.lines.assert_callable` first.

The parts of 1 and 2 that AWS documents are console-driven. They are creatable through the API
but the published guidance is a click-path, so expect the first attempt to be wrong in a way
the console would have made obvious.

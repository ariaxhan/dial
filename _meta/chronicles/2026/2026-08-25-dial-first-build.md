---
title: Dial, first build through to a working demo
date: 2026-08-25
type: note
status: active
created: 2026-08-25
tags: [dial, hackathon, tcpa, nova-sonic, bedrock, chronicle]
---

# Dial: first build

Entry for the AWS Agents for Humans hackathon (deadline 2026-09-14 5pm PDT). Dial finds
recurring money leaks in a mailbox or statement, then makes the cancellation call the person
has been avoiding. Session-level context is in
`CodingVault/_meta/chronicles/2026/2026-08-25-hackathon-to-spend-incident.md`.

## What was attempted, and what changed

Repo created and public at github.com/ariaxhan/dial, MIT. 138 tests. `scripts/demo.py` runs
the whole product offline: statement in, leaks found, human approves one, call placed, five
retention saves refused, confirmation number out.

Built: leak detection, statement and mailbox ingestion, the TCPA line boundary, the mandate
engine, the call loop with its hold gate, a scripted retention line, and the Nova Sonic
preflight.

## The question that could have killed the concept

An AI voice is an "artificial or prerecorded voice" under the TCPA, and every vendor
compliance guide says such calls always need prior express consent. Reading the statute rather
than the guides resolved it: 47 USC 227(b)(1)(B) reaches only **residential** lines and
227(b)(1)(A)(iii) only **wireless** numbers. Neither reaches a company's published business
line. The guides are written for businesses cold-calling consumers, which is the mirror image
of this product.

That became `lines.py`, the spine rather than a disclaimer. Unknown counts as unsafe, and
there is no path to a dial string that bypasses `assert_callable`.

## Verified live

| Claim | Evidence |
| --- | --- |
| Nova Sonic works on this account | real 16kHz speech in, 85KB audio out, correct transcript, in-character reply |
| Preflight discriminates | `amazon.nova-2-sonic-v1:0` ok in 2.08s; a nonexistent model id ok=False after 45s |
| The demo cancels | `scripts/demo.py`, OBJECTIVE_MET, confirmation `CX-131530`, 4 saves refused |
| Mandate holds under pressure | full call in tests, every save offered and refused, still ends in a cancellation |

## What I got wrong

**Trusted a green suite.** At 85 passing tests I ran the demo against a realistic statement
fixture for the first time and found four defects at once, every one demo-fatal: descriptor
variants not merging so one gym read as two vendors, a one-off double charge annualised 12x
into a $4,947 headline, Comcast counted twice, and every subscription labelled unused when
nothing in the data could show use. The headline went from a nonsense $7,728 a year to $1,696.

The fourth is the one worth keeping: the detector was making a claim its evidence could not
support, confidently. A bank statement cannot show whether you use Netflix.

**Two silent bugs the tests would never have caught.** Bank statements disagree on whether a
purchase is positive or negative, so the sign convention is now measured from the file rather
than assumed. And the mock line's confirmation number used `hash()`, which Python randomises
per process, so it would have differed between demo takes.

**`BidiAgent.start()` proves nothing.** A deliberately nonexistent model id reports STARTED
exactly like a real one, because it never contacts AWS. Anything treating session startup as a
health check is reading a signal that does not discriminate, and a misconfigured Sonic is
silent on a live call rather than raising. Hence a preflight that sends real audio.

## Deferred

- **Amazon Connect telephony not started.** Chosen over Twilio deliberately, knowing it is the
  heavier build. Nova Sonic is proven; the media bridge is not written. 20 days left.
- **Certified letter escalation.** The call loop already returns `REFUSED_BY_PHONE` and marks
  it as needing escalation, so the hook exists and the other side does not.
- **Gmail OAuth stays in Testing mode.** Gmail read scopes are Restricted and production
  verification takes longer than the hackathon lasts. Stated in the README rather than left
  for a judge to find.
- **Submission package**: architecture diagram as a standalone image, video, Devpost writeup.
  Presentation is a fifth of the score and is the easiest thing to leave until the last week.

## Worth inheriting

Aria approved one real call to a real business, with her seeing the mandate and the number
before it dials. That approval is per-call, not standing. The mock retention line exists so the
video is repeatable and nobody is recorded without consenting; do not quietly swap it for a
real counterparty on camera.

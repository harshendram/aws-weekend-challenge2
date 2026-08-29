# Weekend Showcase Challenge: Model Drift Radar

**Tags:** #application

**Live dashboard:** https://m4lvje4llsqr6q72k5zqkx5roe0kfqpr.lambda-url.us-east-1.on.aws/
**Repo:** https://github.com/harshendram/aws-weekend-challenge2

---

## The bug that wrote this project

I started the weekend planning to build a model evaluation harness. Twenty
minutes in, the harness told me something I did not want to hear: **every
Bedrock model in my account, in every region, was returning
`ValidationException: Operation not allowed`.**

I assumed I had broken something. I hadn't. All 151 on-demand inference quotas
on the account read `0`, and were marked non-adjustable — which is not a rate
limit you wait out, it means the account is not entitled to Bedrock inference
at all.

Then I checked the agent I shipped for an earlier weekend challenge, a thing
that wakes up daily and generates a frame of a film. It was still running.
Still green. Still writing output. And every single invocation had been failing
its model call and falling back to a hand-written non-AI code path. It had been
degrading for days. Nothing alerted, because "my dependency is still callable"
is not a metric anybody emits.

That reframed the whole project. Model drift is usually told as a story about
quality: the model changes, your prompts quietly stop working. That story is
real. But the *duller* failure is worse and more common — the model is still in
the docs, still in the catalog, still in your config, and your account can no
longer call it.

So **Model Drift Radar** watches both halves.

## What it does

**Reachability.** Every day it diffs the Bedrock catalog across four regions,
then probes every dependency with a one-token call and records whether it
answered. It compares that against the previous scan and raises events —
`ACCESS_LOST`, `MODEL_ADDED`, `MODEL_REMOVED`, `ENTITLEMENT_CHANGED`,
`PRICE_CHANGED` — publishing to SNS only when something actually changed. A
daily "all clear" email is a daily "ignore me" email.

The probe is deliberately the authority. The catalog will happily list models
you are not entitled to invoke, and the Service Quotas API is slow and
occasionally returns `408`. The only answer that cannot be argued with is an
actual call.

**Behaviour.** A contract is a JSON file: some inputs, and the properties the
output must hold. The radar replays it across every target, three times each,
and grades the result into tiers — `SWITCH` (passes and cheaper), `SAFE`,
`BASELINE`, `RISKY`, `FAIL`, `ERROR`.

Two decisions I would defend in a review:

**Tiers are assigned by code, never by a model.** A model is allowed to
*rewrite* the explanation more fluently, and only if one is reachable. Letting
an LLM grade the migration would rest the tool's central claim on the exact
thing it exists to test. As it happens, my account proved the point: with zero
generative models available, the radar is still fully useful.

**Flakiness is graded separately from failure.** A check that never passes is
broken and you will notice within a day. A check that passes two times in three
survives every manual spot-check you will ever run, then fails in production.
Averaging those into one "pass rate" destroys the most valuable signal the tool
produces, so a sometimes-passing check is `RISKY` on its own.

## How I built it, and the pivot

The original design was Bedrock-only. When Bedrock inference turned out to be
unavailable, I had roughly a day, so the fix had to be structural rather than
cosmetic.

I introduced a **target grammar**:

```
provider ":" op [ "@" region ] [ "?" k=v & k=v ]

bedrock:us.amazon.nova-lite-v1:0     foundation model, via Converse
comprehend:redact@ap-south-1         managed PII redaction, in Mumbai
comprehend:pii?min=0.99              same service, stricter threshold
translate:fr?back=en                 translate out, and back again
```

The trick that made the pivot cheap: **every provider normalises its answer to
text**, emitting JSON wherever the service returns structure. The assertion
library never learns that providers exist. My existing `pii-redaction` contract,
written for a foundation model, ran unchanged against Amazon Comprehend the
moment the provider existed — because both hand back plain redacted text.

That turned a dead end into a better product. The radar now compares the *same*
managed service across regions and confidence thresholds, which is the knob
teams actually turn in production, usually without measuring what it costs them.

The hardest part was resisting the urge to fake it. Pricing comes from the live
AWS Price List API, per region, for all three services. If a target cannot be
priced, its cost is `None` — never `0`, never a guess, because an unknown cost
silently read as free produces confidently wrong migration advice.

One bug worth confessing: my price fetcher was silently dropping
`DetectSentiment`. My exclusion regex filtered usage types containing `time`, to
skip training and endpoint dimensions. `DetectSen`**`time`**`nt` matched. It cost
me twenty minutes and is now anchored on hyphenated suffixes.

## What it actually found

These are live results, not hypotheticals:

- **Comprehend classifies `14:32` as `DATE_TIME` at 0.9998 confidence, in every
  region.** A log-scrubbing pipeline that redacts everything Comprehend detects
  will silently destroy the timestamps in its own operational logs.
- **Raising the PII confidence threshold makes redaction worse.** At
  `min=0.999` the detector falls from 24/27 checks to 18/27 and lands in `FAIL` —
  it stops finding real emails and card numbers. The intuitive "be stricter to
  be safer" knob trades a small precision gain for a much larger recall loss.
- **The same managed model is 15× faster in one region than another.** Sentiment
  classification: `ap-south-1` p50 **106 ms** vs `us-east-1` p50 **1571 ms**,
  identical requests, identical published price.
- **Translation cost depends on the target language.** A Japanese round trip
  bills `$1.50` per 1k calls against `$2.17` for German, because Translate bills
  per character and languages are not equally dense.

## Architecture

```
EventBridge (daily 09:00 UTC) ──{"mode":"sweep"}──▶ radar-agent (Lambda)
                                                        │
   Bedrock control plane ── catalog diff, 4 regions ◀────┤
   Bedrock runtime ─────── one-token probes ────────────┤
   Comprehend / Translate ─ contract execution ─────────┤
                                                        │
   DynamoDB ── runs, snapshots, change events ◀─────────┤
   S3 (private) ── raw per-attempt output ◀─────────────┤
   SNS ── alerts, only on real change ◀─────────────────┘

   radar-api (Lambda + Function URL) ── dashboard + read-only JSON
```

**AWS services:** Lambda, DynamoDB, S3, EventBridge, SNS, IAM, Bedrock (control
plane and runtime), Comprehend, Translate, Service Quotas, and the AWS Price
List API.

No SAM, no CDK, no bootstrap stack — `infra/deploy.sh` is idempotent and uses
nothing but the AWS CLI. The dashboard HTML is served from the same Lambda that
serves the JSON, which means one origin, no CORS, no public bucket, and no
CloudFront propagation wait. The single write path is guarded by a shared
secret, so a public URL can never spend my money.

## What I learned across the summer

June taught me to ship something small and finish it. July taught me that a
creative app lives or dies on its *constraints*, not its cleverness. August's
autonomous agent taught me the thing that produced this project: **an agent that
runs unattended will fail unattended too, and it will do it quietly.**

The deeper lesson landed this weekend. I had been treating "the model works" as
a static fact established once at build time. It is not a fact, it is a
*measurement*, and measurements go stale. Access is revocable. Prices move.
Managed models are updated underneath you. Regional endpoints disagree. None of
that shows up in an error rate graph, because nothing errors — something else
just quietly answers instead.

I also learned to build the escape hatch before I need it. The provider
abstraction that rescued this project took about an hour, and only because the
assertion layer had no idea what a model was. Loose coupling stopped being a
principle I nod along to and became the reason I had a submission at all.

## Tagging a builder

**@Lewis Sawe** — *The Museum That Grows* was the build that made me rethink
what "autonomous" should mean. An agent that wakes daily, reads everything it
has already made, and adds one artifact that genuinely fits is a much harder
constraint than generating something new each time, and it is the reason I made
my radar diff against its own previous state instead of just reporting today's
numbers. Thank you for the idea.

---

*Built for the AWS Builder Center Weekend Showcase Challenge, August 2026.*

# Model Drift Radar

**An always-on AWS agent that answers two questions your monitoring does not:
can this account still *call* the models it depends on, and do those models
still *behave* the way your contract says?**

Built for the AWS Builder Center *Weekend Showcase Challenge*, August 2026.

---

## The problem

Model drift is usually told as a story about quality: the model gets updated,
your prompts quietly stop working, and nobody notices until a customer does.

That story is real, but it is not the common failure. The common failure is
duller and worse:

> The model is still in the docs. Still in the catalog. Still in your config.
> And your account can no longer call it.

Nothing throws at deploy time. Your fallback path takes over. Quality drops to
whatever the fallback does, and the dashboard that would have shown you never
existed, because "can I still call my dependency?" is not a metric anyone
emits.

I know that failure is real because **this project found it in my own AWS
account, while I was building it.** Every Bedrock model in every region was
returning `ValidationException: Operation not allowed`. All 151 on-demand
inference quotas read `0`, and were marked non-adjustable. A separate agent I
had running on the same account had been silently degrading for days, falling
back to a non-AI code path on every single invocation, and logging it nowhere
anyone would look.

So the radar watches both halves: **reachability** and **behaviour**.

---

## What it does

### 1. Reachability scan

Every day it asks, per region:

| Question | How |
| --- | --- |
| What is in the catalog? | `ListFoundationModels` + `ListInferenceProfiles` |
| What may this account actually call? | a one-token probe of every watched target |
| Is Bedrock inference available at all? | probe evidence, corroborated by Service Quotas |
| What does it cost? | the AWS Price List API, per region |

It diffs that against the previous scan and raises events — `ACCESS_LOST`,
`MODEL_ADDED`, `MODEL_REMOVED`, `ENTITLEMENT_CHANGED`, `PRICE_CHANGED` — then
publishes anything that changed to SNS. A quiet day sends nothing, because a
daily "all clear" email is a daily "ignore me" email.

The probe is the authority. The catalog lists models you are not entitled to
invoke, and the quota API is slow and occasionally returns `408`, so the only
answer that cannot be argued with is an actual call.

### 2. Behaviour contracts

A contract is a JSON file: some inputs, and the properties the output must
hold. The radar replays it across every target, several times each, and grades
the result.

```json
{
  "id": "pii-redaction",
  "targets": ["comprehend:redact@us-east-1", "comprehend:redact@ap-south-1"],
  "baseline": "comprehend:redact@us-east-1",
  "repetitions": 3,
  "cases": [{
    "id": "over-redaction-guard",
    "input": "The deployment failed at 14:32 UTC because the us-east-1 load balancer rejected the health check on port 8080.",
    "assert": [
      { "type": "contains_any", "values": ["us-east-1"] },
      { "type": "not_contains", "values": ["[REDACTED]"] }
    ]
  }]
}
```

Verdicts are assigned by **code**, never by a model:

| Tier | Meaning |
| --- | --- |
| `SWITCH` | Meets the contract and costs less than the baseline |
| `SAFE` | Meets the contract, but is not cheaper |
| `BASELINE` | Your reference target |
| `RISKY` | One assertion class always fails, **or** a check is flaky |
| `FAIL` | More than one assertion class always fails |
| `ERROR` | Could not be called at all |

Letting an LLM grade the migration would rest the tool's core claim on the very
thing it exists to test. A model is allowed to *rewrite* the explanation more
fluently, and only if one is reachable — the tool is fully usable with no
generative model at all.

**Flakiness is graded separately from failure**, because they are different
problems. A check that never passes is broken and you will notice. A check that
passes two times in three survives every manual spot-check you will ever do,
and fails in production.

---

## Targets: one interface over several AWS services

A *target* is the string you put in a contract:

```
bedrock:us.amazon.nova-lite-v1:0        foundation model, via Converse
comprehend:redact@ap-south-1            managed PII redaction, in Mumbai
comprehend:pii?min=0.99                 same service, stricter threshold
translate:fr?back=en                    translate out and back again
```

Grammar: `provider ":" op [ "@" region ] [ "?" k=v & k=v ]`

Every provider normalises its answer to **text**, emitting JSON wherever the
service returns structure. That is the whole trick: the assertion library never
learns that providers exist, so a contract written against a foundation model
can be pointed at a managed service without changing a line of it — and both
can be scored side by side in the same run.

---

## What it found

Real results from the live deployment, not hypotheticals.

**Amazon Comprehend classifies `14:32` as `DATE_TIME` at 0.9998 confidence, in
every region.** A log-scrubbing pipeline that redacts everything Comprehend
detects will silently destroy the timestamps in its own operational logs. The
`pii-redaction` contract catches this as an over-redaction failure.

**Raising the PII confidence threshold makes redaction worse, not safer.** At
`min=0.999` the detector drops from 24/27 checks to 18/27 and lands in `FAIL` —
it stops finding real emails and phone numbers. The intuitive "be stricter to be
safer" knob turns out to trade a precision gain for a much larger recall loss.

**The same managed model is 15× faster in one region than another.** Sentiment
classification: `ap-south-1` p50 **106 ms**, `us-east-1` p50 **1571 ms**, for
byte-identical requests at an identical published price.

**Translation cost depends on the target language, not just the volume.** A
Japanese round trip bills `$1.50` per 1k calls against `$2.17` for German,
because Translate bills per character and languages are not equally dense.

**Comprehend's 3-unit minimum charge dominates short-text workloads.** Any
request under 300 characters costs exactly the same as a 300-character one, so
half of a short-text bill is floor, not usage.

---

## Architecture

```
EventBridge (daily 09:00 UTC)
        │  {"mode":"sweep"}
        ▼
   radar-agent  (Lambda, python3.13, 600s)
        │
        ├── Bedrock control plane ──── catalog, per region
        ├── Bedrock runtime ─────────── one-token reachability probes
        ├── Comprehend / Translate ──── contract execution
        │
        ├── DynamoDB (driftradar) ───── runs, snapshots, change events
        ├── S3 (private) ───────────── raw per-attempt output
        └── SNS (radar-alerts) ─────── only when something changed

   radar-api  (Lambda + Function URL)
        └── dashboard HTML + read-only JSON, one origin, no CORS
```

**AWS services used:** Lambda, DynamoDB, S3, EventBridge, SNS, IAM, Bedrock
(control plane + runtime), Comprehend, Translate, Service Quotas, and the AWS
Price List API.

Design notes worth calling out:

- **No SAM, no CDK, no bootstrap stack.** `infra/deploy.sh` is idempotent and
  uses nothing but the AWS CLI. Re-run it as often as you like.
- **Prices are fetched, never hardcoded.** A stale price table would produce
  confidently wrong migration advice, which is worse than none. If a target
  cannot be priced, its cost is `None` — never `0`, never a guess.
- **One public write path**, guarded by a shared secret, so a public Function
  URL can never spend AWS money.
- **The private S3 bucket is served through the API Lambda**, so there is no
  public bucket and no CloudFront propagation wait.

---

## Running it

```bash
# What can this account actually call, right now?
python scripts/probe_models.py

# Prove the scoring engine is honest - no AWS access needed
python scripts/selftest.py

# Run one contract locally, against real AWS services
python scripts/local_run.py contracts/pii-detection.json

# Refresh the price table from the AWS Price List API
python scripts/fetch_pricing.py

# Deploy everything
bash infra/deploy.sh
```

### Self-test

`scripts/selftest.py` stubs only the transport and drives the real engine,
assertions, pricing, verdict rules and reachability diff with scripted
responses. It asserts that a clean target passes, a two-class breakage is
`FAIL`, a one-class breakage is `RISKY`, a sometimes-complies target is caught
as flaky rather than averaged into looking fine, a **broken baseline is called
out instead of silently blessing the table**, losing access to a dependency
raises a `critical` event, and Comprehend's 3-unit floor is applied. An
evaluation tool whose own failures are silent is worse than no tool.

---

## Layout

```
contracts/              behaviour contracts (JSON)
infra/deploy.sh         idempotent AWS CLI deploy
lambda/agent/
  providers.py          target grammar + one interface over 3 AWS services
  reach.py              reachability scan, entitlement, catalog diff
  engine.py             contract execution and scorecard aggregation
  assertions.py         deterministic + judged assertion library
  verdict.py            tier rules (code, not a model) and written notes
  pricing.py            per-provider cost, from the live price table
  store.py              DynamoDB + S3 persistence
  handler.py            scan / run / sweep entry points
lambda/api/             read-only JSON API + dashboard, one Function URL
scripts/                probe, local run, self-test, price fetch, packaging
```

---

## Licence

MIT.

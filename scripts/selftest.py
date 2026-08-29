"""Offline proof that the radar is trustworthy.

An evaluation tool whose own failures are silent is worse than no tool. This
drives the REAL engine, assertions, pricing, verdict rules and reachability
diff with scripted responses, so we can assert that:

  * a clean target passes, and a cheaper clean target is promoted to SWITCH,
  * a target that breaks two assertion classes FAILS,
  * a target that breaks exactly one is RISKY, not FAIL,
  * a target that only sometimes complies is caught as FLAKY rather than
    being averaged into looking fine,
  * a broken baseline is called out instead of silently blessing the table,
  * losing access to a dependency raises a critical event,
  * Comprehend's 3-unit minimum charge is applied.

No AWS access required - the transport is stubbed, everything else is the
production code path.

    python scripts/selftest.py
"""
from __future__ import annotations

import json
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda", "agent"))

import engine  # noqa: E402
import pricing  # noqa: E402
import reach  # noqa: E402
import verdict as verdict_mod  # noqa: E402
from assertions import check_deterministic  # noqa: E402
from reply import Reply  # noqa: E402

GOOD = '{"severity": "P1", "category": "billing", "needs_human": true}'
FENCED = '```json\n{"severity": "P1", "category": "billing"}\n```'
OFF_ENUM = '{"severity": "CRITICAL", "category": "billing", "needs_human": true}'

MICRO = "bedrock:us.amazon.nova-micro-v1:0"
LITE = "bedrock:us.amazon.nova-lite-v1:0"
LITE2 = "bedrock:us.amazon.nova-2-lite-v1:0"
PRO = "bedrock:us.amazon.nova-pro-v1:0"
PREMIER = "bedrock:us.amazon.nova-premier-v1:0"
DEAD = "bedrock:us.amazon.nova-imaginary-v1:0"

BEHAVIOUR = {MICRO: "clean", LITE: "off_enum", LITE2: "flaky",
             PRO: "clean", PREMIER: "fenced", DEAD: "dead"}

_counts: dict[tuple[str, str], int] = {}
_lock = threading.Lock()

FAILURES: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    print(f"   {'ok  ' if condition else 'FAIL'} {label}"
          + (f"  {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


def fake_invoke(target, text, contract=None):
    with _lock:
        key = (target, text)
        n = _counts.get(key, 0)
        _counts[key] = n + 1

    mode = BEHAVIOUR.get(target, "clean")
    if mode == "dead":
        return Reply(ok=False, error_code="ValidationException",
                     error="Operation not allowed")
    if mode == "clean":
        body = GOOD
    elif mode == "fenced":
        body = FENCED
    elif mode == "off_enum":
        body = OFF_ENUM
    else:  # flaky: complies on the first two repetitions, slips on the third
        body = GOOD if n < 2 else FENCED

    return Reply(ok=True, text=body, in_tokens=120, out_tokens=28,
                 chars=len(text), latency_ms=400 + 100 * len(target) % 300)


# ---------------------------------------------------------------- suites --

def test_scoring() -> dict:
    print("\nscoring and verdict tiers")
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "contracts", "ticket-triage.json"),
              encoding="utf-8") as fh:
        contract = json.load(fh)

    targets = [MICRO, LITE, LITE2, PRO, PREMIER, DEAD]
    run = engine.run_contract(contract, targets)
    run = verdict_mod.build(run, baseline_target=PRO, with_prose=False)
    tiers = {v["target"]: v for v in run["verdicts"]}

    expected = {
        MICRO: "SWITCH",     # clean and cheaper than the pro baseline
        PRO: "BASELINE",
        LITE: "RISKY",       # exactly one failing class (json_path_in)
        LITE2: "RISKY",      # flaky across repetitions
        PREMIER: "FAIL",     # json_valid AND no_markdown_fence both fail
        DEAD: "ERROR",       # never answered at all
    }
    for target, want in expected.items():
        got = tiers[target]["tier"]
        check(got == want, f"{tiers[target]['label']:<28} -> {want}",
              "" if got == want else f"got {got}")

    check(bool(tiers[LITE2]["flaky"]), "flaky target reported as flaky",
          f"{len(tiers[LITE2]['flaky'])} check(s)")
    check(not tiers[MICRO]["flaky"], "clean target not reported as flaky")
    check(run["baseline_healthy"] is True, "healthy baseline recognised")
    return tiers


def test_broken_baseline() -> None:
    print("\na broken baseline must not silently bless the table")
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "contracts", "ticket-triage.json"),
              encoding="utf-8") as fh:
        contract = json.load(fh)

    run = engine.run_contract(contract, [PREMIER, MICRO])
    run = verdict_mod.build(run, baseline_target=PREMIER, with_prose=False)
    base = next(v for v in run["verdicts"] if v["target"] == PREMIER)

    check(run["baseline_healthy"] is False, "run flagged as unhealthy baseline")
    check(any("baseline itself" in r for r in base["reasons"]),
          "baseline's own failures named in its reasons")


def test_pricing(tiers: dict) -> None:
    print("\npricing")
    micro = tiers[MICRO]["cost_per_1k_calls"]
    pro = tiers[PRO]["cost_per_1k_calls"]
    check(bool(micro and pro and micro < pro),
          "nova-micro priced below nova-pro", f"${micro} vs ${pro} per 1k")

    # Comprehend's floor: a 5-character request still bills three units, and
    # so does a 250-character one, because 250 rounds up to exactly 3 units.
    # Anything above 300 characters finally escapes the floor.
    tiny = pricing.cost_usd("comprehend:pii@us-east-1", Reply(ok=True, chars=5))
    at_floor = pricing.cost_usd("comprehend:pii@us-east-1", Reply(ok=True, chars=250))
    big = pricing.cost_usd("comprehend:pii@us-east-1", Reply(ok=True, chars=900))
    check(tiny is not None and abs(tiny - 0.0003) < 1e-9,
          "3-unit minimum charged on a tiny request", f"${tiny:.6f}")
    check(at_floor == tiny, "250 chars still bills the 3-unit floor")
    check(big is not None and abs(big - 0.0009) < 1e-9,
          "900 chars bills 9 units, escaping the floor", f"${big:.6f}")

    unknown = pricing.cost_usd("bedrock:not.a.real.model", Reply(ok=True))
    check(unknown is None, "an unpriceable target costs None, never zero")


def test_assertions() -> None:
    print("\nassertion library")
    body = '{"types": ["EMAIL", "PHONE"], "count": 2, "scores": {"Negative": 0.97}}'
    cases = [
        ({"type": "json_path_includes", "path": "types",
          "values": ["EMAIL", "PHONE"]}, True, "includes both present types"),
        ({"type": "json_path_includes", "path": "types",
          "values": ["EMAIL", "SSN"]}, False, "includes catches a missing type"),
        ({"type": "json_path_excludes", "path": "types",
          "values": ["SSN"]}, True, "excludes passes when absent"),
        ({"type": "json_path_excludes", "path": "types",
          "values": ["EMAIL"]}, False, "excludes catches an unwanted type"),
        ({"type": "json_len_between", "path": "types", "min": 0, "max": 1},
         False, "len_between catches an over-long list"),
        ({"type": "json_num_at_least", "path": "scores.Negative", "value": 0.9},
         True, "num_at_least reads a nested number"),
        ({"type": "json_num_at_least", "path": "scores.Negative", "value": 0.99},
         False, "num_at_least enforces the floor"),
        ({"type": "json_num_at_least", "path": "scores.Missing", "value": 0.1},
         False, "a missing path fails rather than passing vacuously"),
    ]
    for spec, want, label in cases:
        got = check_deterministic(spec, body, 10)
        check(got.passed == want, label, got.detail)


def test_reach_diff() -> None:
    print("\nreachability diff")
    old = {
        "regions": {"us-east-1": {"on_demand": ["a.model"], "profiles": ["us.b.model"]}},
        "entitlement": {"bedrock:x": {"reachable": True, "detail": "OK"},
                        "comprehend:pii@us-east-1": {"reachable": True, "detail": "OK"}},
        "bedrock_inference": {"verdict": "OK"},
        "rates": {"comprehend:pii@us-east-1": {"unit": "per 100 chars", "input": 0.0001}},
    }
    new = {
        "checked_at": "2026-08-30T00:00:00Z",
        "regions": {"us-east-1": {"on_demand": ["a.model", "c.model"], "profiles": []}},
        "entitlement": {"bedrock:x": {"reachable": False,
                                      "detail": "ValidationException: Operation not allowed"},
                        "comprehend:pii@us-east-1": {"reachable": True, "detail": "OK"}},
        "bedrock_inference": {"verdict": "NO_INFERENCE", "detail": "all quotas zero"},
        "rates": {"comprehend:pii@us-east-1": {"unit": "per 100 chars", "input": 0.0002}},
        "counts": {"us-east-1": 2},
        "reachable": ["comprehend:pii@us-east-1"],
        "unreachable": ["bedrock:x"],
    }
    kinds = {e["kind"]: e for e in reach.diff(old, new)}

    check("ACCESS_LOST" in kinds, "losing access to a dependency is detected")
    check(kinds.get("ACCESS_LOST", {}).get("severity") == "critical",
          "lost access is critical, not a warning")
    check("MODEL_ADDED" in kinds, "a new catalog model is detected")
    check("MODEL_REMOVED" in kinds, "a withdrawn catalog model is detected")
    check("ENTITLEMENT_CHANGED" in kinds, "an entitlement downgrade is detected")
    check("PRICE_CHANGED" in kinds, "a price change is detected")

    quiet = reach.diff(new, new)
    check(quiet == [], "an unchanged world produces no events")
    check(reach.diff(None, new)[0]["kind"] == "FIRST_SCAN",
          "the very first scan records a baseline instead of alerting")


def main() -> int:
    engine.providers.invoke = fake_invoke
    verdict_mod.providers.invoke = fake_invoke

    tiers = test_scoring()
    test_broken_baseline()
    test_pricing(tiers)
    test_assertions()
    test_reach_diff()

    print("\n" + ("ALL CHECKS PASSED" if not FAILURES
                  else f"{len(FAILURES)} CHECK(S) FAILED: " + ", ".join(FAILURES)))
    print()
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())

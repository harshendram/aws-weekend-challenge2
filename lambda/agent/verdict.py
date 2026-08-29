"""Turn a scorecard into a migration verdict.

Deliberate split of responsibility:
  * CODE decides the tier and writes a defensible sentence. Deterministic,
    auditable, reproducible - you can reread the rule that produced it.
  * A MODEL, if one is reachable, rewrites that sentence more fluently. It is
    never allowed to change the verdict, and the tool is fully usable with no
    generative model at all.

Letting an LLM grade the migration would make the tool's core claim rest on
the very thing it is supposed to be testing.
"""
from __future__ import annotations

import providers

BASELINE = "BASELINE"
SWITCH = "SWITCH"
SAFE = "SAFE"
RISKY = "RISKY"
FAIL = "FAIL"
ERROR = "ERROR"

TIER_RANK = {SWITCH: 0, BASELINE: 1, SAFE: 2, RISKY: 3, FAIL: 4, ERROR: 5}

TIER_HELP = {
    SWITCH: "Meets the contract and costs less than the baseline.",
    BASELINE: "Your current reference target.",
    SAFE: "Meets the contract, but is not cheaper than the baseline.",
    RISKY: "One assertion class fails, or a check is flaky across repetitions.",
    FAIL: "More than one assertion class fails. Do not migrate.",
    ERROR: "The target could not be called at all.",
}


def assign_tier(score: dict, baseline: dict | None) -> tuple[str, list[str]]:
    reasons: list[str] = []

    if score["calls_ok"] == 0:
        why = score["call_errors"][0] if score["call_errors"] else "unknown error"
        return ERROR, [f"every call failed ({why[:90]})"]

    if baseline is not None and score["target"] == baseline["target"]:
        # A baseline is exempt from grading, not from scrutiny. If the
        # reference itself breaks the contract then every "no worse than
        # baseline" verdict in the table is measured against a broken ruler,
        # and that has to be said out loud rather than hidden by the tier.
        reasons = ["reference target for this comparison"]
        if score.get("hard_failing"):
            reasons.append("baseline itself always fails "
                           + ", ".join(score["hard_failing"]))
        if score["flaky"]:
            reasons.append(f"baseline itself has {len(score['flaky'])} "
                           f"flaky check(s)")
        return BASELINE, reasons

    # Grade on assertions that NEVER pass. A class that passes intermittently
    # is a flake, and a flake is never worse than RISKY - the target can do the
    # right thing, it just cannot be relied on to.
    hard = score.get("hard_failing", score["failing_types"])
    if hard:
        reasons.append("always fails " + ", ".join(hard))
    if score["flaky"]:
        reasons.append(f"{len(score['flaky'])} flaky check(s): "
                       + ", ".join(score["flaky"][:3]))

    if len(hard) > 1:
        return FAIL, reasons
    if len(hard) == 1 or score["flaky"]:
        return RISKY, reasons

    reasons.append(f"passes all {score['checks_total']} checks")

    if baseline is None:
        return SAFE, reasons + ["no baseline to compare cost against"]

    if score["pass_rate"] < baseline["pass_rate"]:
        return RISKY, reasons + ["pass rate below baseline"]

    mine, theirs = score["cost_per_1k_calls"], baseline["cost_per_1k_calls"]
    if mine is None or theirs is None:
        # Unknown cost must never be read as free.
        return SAFE, reasons + ["cost unknown - cannot compare"]
    if mine < theirs:
        saving = (1 - mine / theirs) * 100 if theirs else 0
        return SWITCH, reasons + [f"{saving:.0f}% cheaper than baseline"]
    return SAFE, reasons + ["not cheaper than baseline"]


def _money(value) -> str:
    if value is None:
        return "an unknown amount"
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def written_note(score: dict, baseline: dict | None, tier: str,
                 reasons: list[str]) -> str:
    """The always-available explanation. No model required."""
    label = score["label"]
    passed = f"{score['checks_passed']}/{score['checks_total']} checks"
    speed = (f"p50 {score['latency_p50']}ms" if score["latency_p50"]
             else "no timing")
    money = _money(score["cost_per_1k_calls"]) + " per 1k calls"

    if tier == ERROR:
        return f"{label} could not be called at all: {reasons[0]}."

    head = f"{label} passed {passed} at {speed}, {money}."

    if tier == SWITCH and baseline:
        return (head + f" It satisfies every assertion in this contract and "
                       f"undercuts {baseline['label']}, so it is a safe swap on "
                       f"both correctness and cost.")
    if tier == BASELINE:
        head += " This is the reference every other row is judged against."
        if score.get("hard_failing"):
            head += (f" It does not satisfy this contract either - it always "
                     f"fails {', '.join(score['hard_failing'])} - so treat "
                     f"every comparison below as relative, not as approval.")
        return head
    if tier == SAFE:
        return head + " It meets the contract, so correctness is not the reason " \
                      "to stay - cost is."
    if tier == RISKY:
        detail = "; ".join(reasons[:2])
        return (head + f" Do not migrate without a guard: {detail}. Assert the "
                       f"failing property in your own code before trusting it.")
    if tier == FAIL:
        return (head + f" It breaks {len(score['hard_failing'])} assertion classes "
                       f"outright ({', '.join(score['hard_failing'][:3])}) and is "
                       f"not a candidate for this workload.")
    return head


def _facts(score: dict, baseline: dict | None) -> str:
    lines = [
        f"target: {score['label']}",
        f"checks passed: {score['checks_passed']}/{score['checks_total']}"
        f" ({score['pass_rate'] * 100:.0f}%)",
        f"latency p50: {score['latency_p50']}ms",
        f"cost per 1k calls: {_money(score['cost_per_1k_calls'])}",
    ]
    if score["failing_types"]:
        detail = []
        for kind in score["failing_types"]:
            s = score["by_type"][kind]
            detail.append(f"{kind} ({s['passed']}/{s['total']} passed)")
        lines.append("failing assertions: " + "; ".join(detail))
    else:
        lines.append("failing assertions: none")
    if score["flaky"]:
        lines.append("flaky (passes only sometimes): " + ", ".join(score["flaky"]))
    if baseline is not None and baseline["target"] != score["target"]:
        lines.append(f"baseline {baseline['label']}: "
                     f"{baseline['pass_rate'] * 100:.0f}% pass, "
                     f"p50 {baseline['latency_p50']}ms, cost per 1k "
                     f"{_money(baseline['cost_per_1k_calls'])}")
    return "\n".join(lines)


def polish(score: dict, baseline: dict | None, tier: str, fallback: str,
           writer_target: str) -> tuple[str, str]:
    """Ask a model to rewrite the note. Returns (note, source).

    If no writer is reachable - which is the normal case on an account without
    generative model access - the deterministic sentence stands unchanged.
    """
    if not writer_target:
        return fallback, "rules"

    prompt = (
        "You are writing one short migration note for an engineer.\n"
        f"The verdict has ALREADY been decided by rules: {tier}.\n"
        "Do not dispute it, do not restate the numbers as a list, and do not "
        "use markdown. Write at most two sentences explaining what the data "
        "means practically, and if anything failed, name the concrete guard "
        "the engineer should add.\n\n"
        f"FACTS:\n{_facts(score, baseline)}\n\nNote:"
    )
    reply = providers.invoke(writer_target, prompt,
                             {"max_tokens": 120, "temperature": 0.2})
    if not reply.ok or not reply.text.strip():
        return fallback, "rules"
    return reply.text.strip(), writer_target


def build(run: dict, baseline_target: str, writer_target: str = "",
          with_prose: bool = True) -> dict:
    scores = run["scores"]
    baseline = next((s for s in scores if s["target"] == baseline_target), None)

    verdicts = []
    for score in scores:
        tier, reasons = assign_tier(score, baseline)
        note = written_note(score, baseline, tier, reasons)
        source = "rules"
        if with_prose and writer_target:
            note, source = polish(score, baseline, tier, note, writer_target)
        verdicts.append({
            "target": score["target"],
            "label": score["label"],
            "tier": tier,
            "reasons": reasons,
            "note": note,
            "note_source": source,
            "pass_rate": score["pass_rate"],
            "checks_passed": score["checks_passed"],
            "checks_total": score["checks_total"],
            "latency_p50": score["latency_p50"],
            "latency_p95": score["latency_p95"],
            "cost_per_1k_calls": score["cost_per_1k_calls"],
            "rate_card": score.get("rate_card"),
            "failing_types": score["failing_types"],
            "hard_failing": score.get("hard_failing", []),
            "flaky": score["flaky"],
            "call_errors": score.get("call_errors", []),
        })

    verdicts.sort(key=lambda v: (TIER_RANK[v["tier"]], -v["pass_rate"]))
    run["baseline"] = baseline_target
    run["baseline_healthy"] = bool(
        baseline and not baseline["hard_failing"] and not baseline["flaky"])
    run["verdicts"] = verdicts
    return run

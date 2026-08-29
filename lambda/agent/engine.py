"""Execute a behaviour contract across targets and aggregate a scorecard."""
from __future__ import annotations

import concurrent.futures as cf
import statistics
import time
from dataclasses import dataclass, field

import pricing
import providers
from assertions import (DETERMINISTIC, JUDGED, Outcome, check_deterministic,
                        judge_prompt, parse_judge)

MAX_TARGETS = 8
MAX_CASES = 12
MAX_REPS = 5


def build_prompt(case: dict) -> str:
    context = case.get("context")
    if context:
        return f"CONTEXT:\n{context}\n\nQUESTION:\n{case['input']}"
    return case["input"]


@dataclass
class Attempt:
    """One (case, repetition) against one target."""
    case_id: str
    rep: int
    ok: bool
    output: str
    latency_ms: int
    in_tokens: int
    out_tokens: int
    chars: int
    cost_usd: float | None
    outcomes: list[Outcome] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id, "rep": self.rep, "ok": self.ok,
            "output": self.output, "latency_ms": self.latency_ms,
            "in_tokens": self.in_tokens, "out_tokens": self.out_tokens,
            "chars": self.chars, "cost_usd": self.cost_usd,
            "error": self.error,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


def _evaluate(case: dict, output: str, out_tokens: int,
              judge_target: str) -> list[Outcome]:
    outcomes: list[Outcome] = []
    for spec in case["assert"]:
        kind = spec["type"]
        if kind in DETERMINISTIC:
            outcomes.append(check_deterministic(spec, output, out_tokens))
        elif kind in JUDGED:
            if not judge_target:
                outcomes.append(Outcome(kind, False, "no judge configured"))
                continue
            reply = providers.invoke(
                judge_target, judge_prompt(spec, case, output),
                {"max_tokens": 8, "temperature": 0.0})
            if not reply.ok:
                outcomes.append(Outcome(kind, False,
                                        f"judge unavailable: {reply.error_code}"))
            else:
                outcomes.append(parse_judge(reply.text, kind))
        else:
            outcomes.append(Outcome(kind, False, f"unknown assertion '{kind}'"))
    return outcomes


def _one_attempt(target: str, case: dict, rep: int, contract: dict,
                 judge_target: str) -> Attempt:
    reply = providers.invoke(target, build_prompt(case), contract)

    if not reply.ok:
        return Attempt(case["id"], rep, False, "", reply.latency_ms, 0, 0, 0, None,
                       [Outcome(s["type"], False, f"call failed: {reply.error_code}")
                        for s in case["assert"]],
                       error=f"{reply.error_code}: {reply.error[:160]}")

    return Attempt(
        case_id=case["id"], rep=rep, ok=True, output=reply.text,
        latency_ms=reply.latency_ms, in_tokens=reply.in_tokens,
        out_tokens=reply.out_tokens, chars=reply.chars,
        cost_usd=pricing.cost_usd(target, reply),
        outcomes=_evaluate(case, reply.text, reply.out_tokens, judge_target),
    )


def score_target(target: str, contract: dict, judge_target: str,
                 workers: int = 4, progress=None) -> dict:
    cases = contract["cases"][:MAX_CASES]
    reps = min(contract.get("repetitions", 3), MAX_REPS)

    jobs = [(c, r) for c in cases for r in range(reps)]
    attempts: list[Attempt] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_one_attempt, target, c, r, contract, judge_target): (c, r)
            for c, r in jobs
        }
        for fut in cf.as_completed(futures):
            case, rep = futures[fut]
            try:
                attempts.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                attempts.append(Attempt(case["id"], rep, False, "", 0, 0, 0, 0,
                                        None, [], error=f"crashed: {exc}"))
            if progress:
                progress(target, len(attempts), len(jobs))

    attempts.sort(key=lambda a: (a.case_id, a.rep))
    return aggregate(target, contract, attempts)


def aggregate(target: str, contract: dict, attempts: list[Attempt]) -> dict:
    checks_total = checks_passed = 0
    # (case_id, assertion_type) -> [bool, ...] across repetitions
    per_check: dict[tuple[str, str], list[bool]] = {}
    by_type: dict[str, dict[str, int]] = {}

    for att in attempts:
        for out in att.outcomes:
            checks_total += 1
            checks_passed += bool(out.passed)
            per_check.setdefault((att.case_id, out.type), []).append(out.passed)
            slot = by_type.setdefault(out.type, {"passed": 0, "total": 0})
            slot["total"] += 1
            slot["passed"] += bool(out.passed)

    # A check that passes only sometimes is the single most dangerous result:
    # it survives manual spot-checking and fails in production.
    flaky = sorted({
        f"{case_id}:{kind}" for (case_id, kind), results in per_check.items()
        if 0 < sum(results) < len(results)
    })
    failing_types = sorted(
        t for t, s in by_type.items() if s["passed"] < s["total"])

    # A class that never once passed is broken; a class that passes sometimes
    # is flaky. Grading those the same would erase the most useful signal this
    # tool produces, so they are counted separately.
    hard_failing = sorted({
        kind for (case_id, kind), results in per_check.items()
        if sum(results) == 0
    })

    lat = [a.latency_ms for a in attempts if a.ok and a.latency_ms > 0]
    costs = [a.cost_usd for a in attempts if a.cost_usd is not None]
    calls_ok = sum(1 for a in attempts if a.ok)
    avg_cost = (sum(costs) / len(costs)) if costs else None

    try:
        label = providers.parse(target).short
    except ValueError:
        label = target

    return {
        "target": target,
        "label": label,
        "contract": contract["id"],
        "calls": len(attempts),
        "calls_ok": calls_ok,
        "call_errors": [a.error for a in attempts if not a.ok][:5],
        "checks_total": checks_total,
        "checks_passed": checks_passed,
        "pass_rate": round(checks_passed / checks_total, 4) if checks_total else 0.0,
        "by_type": by_type,
        "failing_types": failing_types,
        "hard_failing": hard_failing,
        "flaky": flaky,
        "latency_p50": int(statistics.median(lat)) if lat else None,
        "latency_p95": int(sorted(lat)[max(0, int(len(lat) * 0.95) - 1)]) if lat else None,
        "avg_cost_usd": avg_cost,
        # The unit people actually reason about when sizing a workload.
        "cost_per_1k_calls": round(avg_cost * 1000, 6) if avg_cost is not None else None,
        "rate_card": pricing.rate_card(target),
        "attempts": [a.to_dict() for a in attempts],
    }


def run_contract(contract: dict, targets: list[str], judge_target: str = "",
                 progress=None) -> dict:
    started = time.time()
    scores = [score_target(t, contract, judge_target, progress=progress)
              for t in targets[:MAX_TARGETS]]
    return {
        "contract": contract["id"],
        "title": contract.get("title", contract["id"]),
        "description": contract.get("description", ""),
        "judge_target": judge_target,
        "started_at": started,
        "duration_s": round(time.time() - started, 1),
        "scores": scores,
    }

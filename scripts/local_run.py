"""Run a behaviour contract locally against real AWS services.

Identical code path to the Lambda - only the entry point differs. Iterate
here, deploy once.

    python scripts/local_run.py contracts/pii-redaction.json
    python scripts/local_run.py contracts/pii-detection.json --targets comprehend:pii@us-east-1
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda", "agent"))

import providers  # noqa: E402
import verdict as verdict_mod  # noqa: E402
from engine import run_contract  # noqa: E402

C = {"SWITCH": "\033[92m", "SAFE": "\033[96m", "BASELINE": "\033[94m",
     "RISKY": "\033[93m", "FAIL": "\033[91m", "ERROR": "\033[91m"}
R, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"


def short(target: str) -> str:
    try:
        return providers.parse(target).short
    except ValueError:
        return target


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("contract")
    ap.add_argument("--targets", default="",
                    help="comma separated; defaults to the contract's own list")
    ap.add_argument("--baseline", default="")
    ap.add_argument("--judge", default="",
                    help="target that scores judged assertions, if any")
    ap.add_argument("--writer", default="",
                    help="optional target that rewrites the migration notes")
    ap.add_argument("--save", default="", help="write full run JSON here")
    args = ap.parse_args()

    with open(args.contract, encoding="utf-8") as fh:
        contract = json.load(fh)

    targets = ([t.strip() for t in args.targets.split(",") if t.strip()]
               or contract.get("targets", []))
    if not targets:
        print("no targets: pass --targets or add a 'targets' list to the contract")
        return 2
    baseline = args.baseline or contract.get("baseline", targets[0])
    judge = args.judge or contract.get("judge", "")

    print(f"\n{BOLD}{contract['title']}{R}")
    print(f"{DIM}{contract['description']}{R}")
    reps = contract.get("repetitions", 3)
    n_assert = sum(len(c["assert"]) for c in contract["cases"])
    print(f"\n{len(contract['cases'])} cases x {reps} reps x {len(targets)} targets"
          f"  ->  {n_assert * reps * len(targets)} assertion checks\n")

    def progress(target, done, total):
        bar = f"  {short(target)}  {done}/{total}"
        print(f"\r{bar:<150}", end="", flush=True)

    run = run_contract(contract, targets, judge, progress=progress)
    print("\r" + " " * 160 + "\r", end="")

    run = verdict_mod.build(run, baseline, args.writer, with_prose=bool(args.writer))

    print(f"{BOLD}{'TARGET':<40}{'TIER':<10}{'PASS':<12}{'p50':>8}{'$/1k calls':>13}{R}")
    print("-" * 96)
    for v in run["verdicts"]:
        col = C.get(v["tier"], "")
        rate = f"{v['checks_passed']}/{v['checks_total']}"
        pct = f"({v['pass_rate'] * 100:.0f}%)"
        p50 = f"{v['latency_p50']}ms" if v["latency_p50"] else "-"
        cost = (f"${v['cost_per_1k_calls']:.4f}"
                if v["cost_per_1k_calls"] is not None else "unpriced")
        print(f"{v['label'][:39]:<40}{col}{v['tier']:<10}{R}"
              f"{rate:<7}{DIM}{pct:<5}{R}{p50:>8}{cost:>13}")
        if v["failing_types"]:
            print(f"{DIM}    fails: {', '.join(v['failing_types'])}{R}")
        if v["flaky"]:
            print(f"\033[93m    FLAKY: {', '.join(v['flaky'])}{R}")
        if v["call_errors"]:
            print(f"{DIM}    error: {v['call_errors'][0][:110]}{R}")
        if v.get("note"):
            print(f"{DIM}    {v['note']}{R}")
    print(f"\n{DIM}baseline={short(run['baseline'])}  {run['duration_s']}s{R}\n")

    if args.save:
        with open(args.save, "w", encoding="utf-8") as fh:
            json.dump(run, fh, indent=2)
        print(f"saved -> {args.save}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

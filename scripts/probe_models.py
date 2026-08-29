"""Answer, definitively, what this AWS account can actually call right now.

    python scripts/probe_models.py                     # watched targets
    python scripts/probe_models.py --catalog           # also dump the catalog
    python scripts/probe_models.py --targets a,b,c

Run this before anything else. The Bedrock catalog will happily list models
you are not entitled to invoke, so the only trustworthy answer comes from
making the call.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda", "agent"))

import reach  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")

DEFAULT_TARGETS = [
    "bedrock:us.amazon.nova-micro-v1:0",
    "bedrock:us.amazon.nova-lite-v1:0",
    "bedrock:us.amazon.nova-pro-v1:0",
    "comprehend:pii@us-east-1",
    "comprehend:sentiment@us-east-1",
    "comprehend:sentiment@ap-south-1",
    "translate:es@us-east-1",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default=",".join(DEFAULT_TARGETS))
    ap.add_argument("--regions", default="us-east-1,us-west-2,eu-west-1,ap-south-1")
    ap.add_argument("--catalog", action="store_true",
                    help="print every text model id per region")
    ap.add_argument("--out", default="", help="write the snapshot to JSON")
    args = ap.parse_args()

    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    regions = [r.strip() for r in args.regions.split(",") if r.strip()]

    print(f"\nScanning {len(regions)} region(s), probing {len(targets)} target(s)...")
    snap = reach.snapshot(regions, targets)

    print(f"\n{'REGION':<14}{'TEXT MODELS':>13}{'  ON-DEMAND':>13}{'  PROFILES':>12}")
    print("-" * 54)
    for region in regions:
        cat = snap["regions"][region]
        if cat.get("error"):
            print(f"{region:<14}{RED}{cat['error'][:38]}{RESET}")
            continue
        print(f"{region:<14}{snap['counts'][region]:>13}"
              f"{len(cat['on_demand']):>13}{len(cat['profiles']):>12}")

    inference = snap["bedrock_inference"]
    colour = GREEN if inference["verdict"] == "OK" else (
        YELLOW if inference["verdict"] in ("PARTIAL", "UNKNOWN") else RED)
    print(f"\nBedrock inference entitlement: {colour}{inference['verdict']}{RESET}")
    print(f"{DIM}   {inference['detail']}{RESET}")

    print(f"\n{'TARGET':<44} RESULT")
    print("-" * 96)
    for target in targets:
        state = snap["entitlement"][target]
        colour = GREEN if state["reachable"] else RED
        mark = "REACHABLE" if state["reachable"] else "UNREACHABLE"
        print(f"{target:<44} {colour}{mark:<12}{RESET} {DIM}{state['detail'][:60]}{RESET}")

    print(f"\n{GREEN}{len(snap['reachable'])}{RESET} of {len(targets)} "
          f"target(s) usable right now.\n")

    if args.catalog:
        for region in regions:
            cat = snap["regions"][region]
            print(f"\n=== {region} ===")
            for mid in cat.get("on_demand", []):
                print(f"   on-demand  {mid}")
            for pid in cat.get("profiles", []):
                print(f"   profile    {pid}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, indent=2)
        print(f"\nWrote snapshot -> {args.out}\n")
    return 0 if snap["reachable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

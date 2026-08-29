"""Model Drift Radar agent.

    {"mode": "scan"}                    -> is everything we depend on still
                                           callable, and what changed?
    {"mode": "run", "contract": "..."}  -> does one contract still hold?
    {"mode": "sweep"}                   -> scan, then run every live contract

One Lambda for all three: they share the provider layer, the store and the
alerting, and splitting them would double the deploy surface for no gain.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import traceback

import boto3
import reach
import store
import verdict as verdict_mod
from engine import run_contract

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACT_DIR = os.path.join(HERE, "contracts")


def _split(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


REGIONS = _split("RADAR_REGIONS", "us-east-1,us-west-2,eu-west-1,ap-south-1")
# The dependencies this account actually claims to have. Probing only these
# keeps a daily scan to a handful of cheap calls instead of hundreds.
WATCH = _split("RADAR_WATCH",
               "bedrock:us.amazon.nova-micro-v1:0,"
               "bedrock:us.amazon.nova-lite-v1:0,"
               "bedrock:us.amazon.nova-pro-v1:0,"
               "comprehend:pii@us-east-1,"
               "comprehend:sentiment@us-east-1,"
               "comprehend:sentiment@ap-south-1,"
               "translate:es@us-east-1")
# Contracts safe to run unattended. A contract whose targets are unreachable
# would just burn a scheduled run producing a table of ERROR rows.
LIVE_CONTRACTS = _split("RADAR_LIVE_CONTRACTS",
                        "pii-redaction,pii-detection,sentiment-stability,"
                        "translate-fidelity")
WRITER = os.environ.get("RADAR_WRITER", "")
TOPIC = os.environ.get("RADAR_TOPIC", "")

_sns = None


def sns():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns", region_name=os.environ.get("RADAR_REGION",
                                                              "us-east-1"))
    return _sns


def notify(subject: str, message: str) -> None:
    if not TOPIC:
        print(f"[notify skipped] {subject}\n{message}")
        return
    try:
        sns().publish(TopicArn=TOPIC, Subject=subject[:99], Message=message)
    except Exception as exc:  # noqa: BLE001
        print(f"[notify failed] {exc}")


def load_contract(name: str) -> dict:
    with open(os.path.join(CONTRACT_DIR, f"{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def available_contracts() -> list[str]:
    if not os.path.isdir(CONTRACT_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(CONTRACT_DIR) if f.endswith(".json"))


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------- modes --

def do_run(name: str, targets: list[str] | None, trigger: str) -> dict:
    contract = load_contract(name)
    targets = targets or contract.get("targets", [])
    if not targets:
        raise ValueError(f"contract '{name}' has no targets")
    baseline = contract.get("baseline", targets[0])

    run = run_contract(contract, targets, contract.get("judge", ""))
    run = verdict_mod.build(run, baseline, WRITER, with_prose=bool(WRITER))
    run["run_id"] = f"{now_iso()}#{name}"
    run["created_at"] = now_iso()
    run["trigger"] = trigger
    run["targets"] = targets
    store.save_run(run)

    attention = [v for v in run["verdicts"] if v["tier"] in ("FAIL", "RISKY", "ERROR")]
    print(f"[run] {run['run_id']} {len(run['verdicts'])} targets, "
          f"{len(attention)} needing attention")
    return run


def do_scan(trigger: str = "schedule") -> dict:
    """Diff today's reachable world against the last one we recorded."""
    current = reach.snapshot(REGIONS, WATCH)
    previous = store.latest_snapshot()
    events = reach.diff(previous, current)

    store.save_snapshot(current)
    if events:
        store.save_events(events, current["checked_at"])

    body = reach.describe(current, events)
    print(f"[scan] {len(events)} event(s); "
          f"{len(current['reachable'])}/{len(current['entitlement'])} reachable")
    print(body)

    worst = "info"
    for ev in events:
        if ev["severity"] == "critical":
            worst = "critical"
            break
        if ev["severity"] == "warn":
            worst = "warn"

    # A daily "nothing changed" email trains people to ignore the channel, so
    # only a real change is worth a notification.
    if events and events[0]["kind"] != "FIRST_SCAN":
        notify(f"Model Drift Radar [{worst}]: {len(events)} change(s) detected",
               body)

    return {"checked_at": current["checked_at"], "events": events,
            "reachable": current["reachable"],
            "unreachable": current["unreachable"],
            "bedrock_inference": current["bedrock_inference"],
            "counts": current["counts"], "trigger": trigger}


def do_sweep() -> dict:
    scan = do_scan(trigger="sweep")
    runs, failed = [], []
    for name in LIVE_CONTRACTS:
        try:
            run = do_run(name, None, trigger="sweep")
            runs.append({"run_id": run["run_id"],
                         "worst": min((v["tier"] for v in run["verdicts"]),
                                      key=lambda t: -verdict_mod.TIER_RANK[t],
                                      default="NONE")})
        except Exception as exc:  # noqa: BLE001 - one bad contract is not fatal
            print(f"[sweep] contract {name} failed: {exc}")
            traceback.print_exc()
            failed.append({"contract": name, "error": str(exc)})
    return {"scan": scan, "runs": runs, "failed": failed}


def lambda_handler(event, context):  # noqa: ARG001
    mode = (event or {}).get("mode", "scan")
    print(f"[agent] mode={mode} event={json.dumps(event or {})[:400]}")
    try:
        if mode in ("scan", "scout"):
            return {"ok": True, "result": do_scan(
                trigger=event.get("trigger", "schedule"))}

        if mode == "sweep":
            return {"ok": True, "result": do_sweep()}

        if mode == "run":
            name = event.get("contract") or (LIVE_CONTRACTS or [None])[0]
            if not name:
                return {"ok": False, "error": "no contract given"}
            run = do_run(name, event.get("targets"),
                         trigger=event.get("trigger", "manual"))
            return {"ok": True, "run_id": run["run_id"],
                    "verdicts": [{k: v[k] for k in ("label", "tier", "pass_rate")}
                                 for v in run["verdicts"]]}

        return {"ok": False, "error": f"unknown mode '{mode}'",
                "modes": ["scan", "run", "sweep"]}
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

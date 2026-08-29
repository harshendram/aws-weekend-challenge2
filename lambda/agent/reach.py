"""Reachability radar: is what this app depends on still callable today?

Behaviour drift is the famous failure. It is not the common one. The common
one is quieter: the dependency is still in the docs, still in the catalog,
still in your config - and your account can no longer call it. Nothing throws
at deploy time. The fallback path takes over. Quality drops, and the graph
that would have shown it does not exist.

So this module answers three questions on a schedule and shouts when an
answer changes:

  1. What is in the catalog?      ListFoundationModels + ListInferenceProfiles
  2. What may this account call?  a one-token probe, which is the only
                                  authority that cannot be argued with
  3. What does it cost?           the published rate for that exact target

Everything here is control-plane except the probes, so a scan is cheap enough
to run daily and honest enough to page on.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import re
import time

import bedrock
import pricing
import providers

# Video, image, embedding and speech models answer a different question than
# "will my text pipeline still work", so they are not tracked here.
_NOT_TEXT = re.compile(
    r"embed|rerank|stable|canvas|reel|sonic|pegasus|marengo|upscale|inpaint",
    re.IGNORECASE)
_VARIANT = re.compile(r":\d+k$|:mm$")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- catalog --

def catalog(region: str) -> dict:
    """Every text model id this region advertises, by how you would call it."""
    on_demand, profiles = [], []
    try:
        page = bedrock.control(region).list_foundation_models(
            byInferenceType="ON_DEMAND")
        for m in page.get("modelSummaries", []):
            mid = m.get("modelId", "")
            if "TEXT" not in m.get("outputModalities", []):
                continue
            if _NOT_TEXT.search(mid) or _VARIANT.search(mid):
                continue
            on_demand.append(mid)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}",
                "on_demand": [], "profiles": []}

    try:
        pages = bedrock.control(region).get_paginator("list_inference_profiles")
        for page in pages.paginate():
            for p in page.get("inferenceProfileSummaries", []):
                pid = p.get("inferenceProfileId", "")
                if _NOT_TEXT.search(pid) or _VARIANT.search(pid):
                    continue
                profiles.append(pid)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}",
                "on_demand": sorted(on_demand), "profiles": []}

    return {"on_demand": sorted(on_demand), "profiles": sorted(profiles),
            "error": ""}


# ------------------------------------------------------------ entitlement --

def quota_survey(region: str, budget_s: float = 25.0) -> dict:
    """How many of this account's Bedrock on-demand quotas are above zero?

    Corroborating evidence, not the verdict. Every on-demand inference quota
    reading zero AND non-adjustable means the account is not entitled to
    Bedrock inference at all - which is a different problem from being
    throttled, and is not one you can retry your way out of.

    ListServiceQuotas over Bedrock pages slowly and sometimes returns 408, so
    this runs on a time budget and is allowed to give up. The probes are the
    authority; this only explains them.
    """
    checked = nonzero = adjustable = 0
    deadline = time.monotonic() + budget_s
    try:
        pages = bedrock.client("service-quotas", region).get_paginator(
            "list_service_quotas")
        for page in pages.paginate(ServiceCode="bedrock",
                                   PaginationConfig={"PageSize": 100}):
            for q in page.get("Quotas", []):
                if not q["QuotaName"].startswith("On-demand model inference"):
                    continue
                checked += 1
                nonzero += q.get("Value", 0) > 0
                adjustable += bool(q.get("Adjustable"))
            if time.monotonic() > deadline:
                return {"checked": checked, "nonzero": nonzero,
                        "adjustable": adjustable, "partial": True,
                        "detail": f"gave up after {budget_s:.0f}s with "
                                  f"{checked} quotas read"}
    except Exception as exc:  # noqa: BLE001
        return {"checked": checked, "nonzero": nonzero, "adjustable": adjustable,
                "partial": True,
                "detail": f"quota survey unavailable ({type(exc).__name__})"}

    if checked == 0:
        return {"checked": 0, "nonzero": 0, "adjustable": 0, "partial": True,
                "detail": "no on-demand inference quotas reported"}
    return {
        "checked": checked, "nonzero": nonzero, "adjustable": adjustable,
        "partial": False,
        "detail": (f"{nonzero} of {checked} on-demand inference quotas are "
                   f"above zero"
                   + ("" if adjustable or nonzero else ", and none are adjustable")),
    }


def bedrock_entitlement(entitlement: dict, quotas: dict) -> dict:
    """Can this account do Bedrock inference at all?

    Decided from the probes, because a probe is the only signal that cannot
    be argued with: the catalog lies by omission, and the quota API is slow
    and occasionally unavailable. Quotas are folded in as explanation.
    """
    probes = {t: e for t, e in entitlement.items() if t.startswith("bedrock:")}
    live = [t for t, e in probes.items() if e["reachable"]]

    if not probes:
        verdict, detail = "UNKNOWN", "no Bedrock targets were probed"
    elif live and len(live) == len(probes):
        verdict, detail = "OK", f"all {len(probes)} probed Bedrock targets answered"
    elif live:
        verdict = "PARTIAL"
        detail = f"{len(live)} of {len(probes)} probed Bedrock targets answered"
    else:
        verdict = "NO_INFERENCE"
        first = next(iter(probes.values()))["detail"]
        detail = (f"none of the {len(probes)} probed Bedrock targets could be "
                  f"called ({first[:80]})")

    if quotas.get("checked"):
        detail += f"; {quotas['detail']}"
    return {"verdict": verdict, "detail": detail, "quotas": quotas}


def probe_targets(targets: list[str], workers: int = 6) -> dict:
    """The only authority that settles callability: actually call it."""
    out: dict[str, dict] = {}

    def one(target: str) -> tuple[str, dict]:
        ok, detail = providers.probe(target)
        return target, {"reachable": ok, "detail": detail}

    with cf.ThreadPoolExecutor(max_workers=min(workers, max(1, len(targets)))) as pool:
        for target, result in pool.map(one, targets):
            out[target] = result
    return out


# --------------------------------------------------------------- snapshot --

def snapshot(regions: list[str], targets: list[str]) -> dict:
    catalogs = {r: catalog(r) for r in regions}
    entitlement = probe_targets(targets) if targets else {}
    rates = {t: pricing.rate_card(t) for t in targets}
    return {
        "checked_at": now_iso(),
        "regions": catalogs,
        "entitlement": entitlement,
        "bedrock_inference": bedrock_entitlement(entitlement,
                                                 quota_survey(regions[0])),
        "rates": rates,
        "counts": {r: len(c["on_demand"]) + len(c["profiles"])
                   for r, c in catalogs.items()},
        "reachable": sorted(t for t, e in entitlement.items() if e["reachable"]),
        "unreachable": sorted(t for t, e in entitlement.items()
                              if not e["reachable"]),
    }


# ------------------------------------------------------------------- diff --

def diff(old: dict | None, new: dict) -> list[dict]:
    """Changes worth waking someone up for. Empty list on a quiet day."""
    if not old:
        return [{"kind": "FIRST_SCAN", "severity": "info",
                 "detail": f"baseline recorded: "
                           f"{sum(new['counts'].values())} text models across "
                           f"{len(new['counts'])} regions, "
                           f"{len(new['reachable'])} of "
                           f"{len(new['entitlement'])} targets reachable"}]

    events: list[dict] = []

    for region, cat in new["regions"].items():
        before = old["regions"].get(region, {})
        was = set(before.get("on_demand", [])) | set(before.get("profiles", []))
        now = set(cat.get("on_demand", [])) | set(cat.get("profiles", []))
        if not was:
            continue
        for mid in sorted(now - was):
            events.append({"kind": "MODEL_ADDED", "severity": "info",
                           "region": region, "subject": mid,
                           "detail": f"{mid} appeared in {region}"})
        for mid in sorted(was - now):
            events.append({"kind": "MODEL_REMOVED", "severity": "warn",
                           "region": region, "subject": mid,
                           "detail": f"{mid} disappeared from {region}"})

    for target, state in new["entitlement"].items():
        was = old["entitlement"].get(target)
        if was is None:
            continue
        if was["reachable"] and not state["reachable"]:
            events.append({"kind": "ACCESS_LOST", "severity": "critical",
                           "subject": target,
                           "detail": f"{target} was callable and is not any "
                                     f"more: {state['detail']}"})
        elif not was["reachable"] and state["reachable"]:
            events.append({"kind": "ACCESS_GAINED", "severity": "info",
                           "subject": target,
                           "detail": f"{target} is callable again"})

    old_verdict = (old.get("bedrock_inference") or {}).get("verdict")
    new_verdict = new["bedrock_inference"]["verdict"]
    if old_verdict and old_verdict != new_verdict:
        worse = new_verdict in ("NO_INFERENCE", "PARTIAL")
        events.append({"kind": "ENTITLEMENT_CHANGED",
                       "severity": "critical" if worse else "info",
                       "subject": "bedrock",
                       "detail": f"Bedrock inference entitlement went "
                                 f"{old_verdict} -> {new_verdict}: "
                                 f"{new['bedrock_inference']['detail']}"})

    for target, rate in new["rates"].items():
        was = (old.get("rates") or {}).get(target)
        if not was or not rate or was == rate:
            continue
        events.append({"kind": "PRICE_CHANGED", "severity": "warn",
                       "subject": target,
                       "detail": f"{target} price moved {was} -> {rate}"})

    return events


def describe(new: dict, events: list[dict]) -> str:
    """The body of the alert. Written to be readable in a phone notification."""
    lines = [f"Model Drift Radar scan {new['checked_at']}", ""]
    inference = new["bedrock_inference"]
    lines.append(f"Bedrock inference entitlement: {inference['verdict']} "
                 f"({inference['detail']})")
    lines.append(f"Targets reachable: {len(new['reachable'])}/"
                 f"{len(new['entitlement'])}")
    for region, count in sorted(new["counts"].items()):
        lines.append(f"  {region}: {count} text models in catalog")

    if new["unreachable"]:
        lines += ["", "Unreachable right now:"]
        for target in new["unreachable"]:
            lines.append(f"  {target} - {new['entitlement'][target]['detail'][:110]}")

    if events:
        lines += ["", f"{len(events)} change(s) since the last scan:"]
        for ev in events:
            lines.append(f"  [{ev['severity'].upper()}] {ev['detail']}")
    else:
        lines += ["", "No change since the last scan."]
    return "\n".join(lines)

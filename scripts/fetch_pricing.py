"""Build pricing.json from the live AWS Price List API.

A hardcoded price table is wrong the moment AWS changes a price - and this
tool's entire output is a cost comparison, so a stale table would quietly
produce wrong migration advice. Pull the real numbers instead.

Three services, because a target can be any of them:
  * Bedrock foundation models, billed per 1M tokens, in and out.
  * Comprehend, billed per 100-character "unit" with a 3-unit floor.
  * Translate, billed per character.

Comprehend and Translate are priced PER REGION, and the radar compares the
same call across regions, so those are fetched for every watched region
rather than assuming us-east-1 holds everywhere.

    python scripts/fetch_pricing.py
"""
from __future__ import annotations

import json
import os
import re
import sys

import boto3

OUT = os.path.join(os.path.dirname(__file__), "..", "lambda", "agent", "pricing.json")
BEDROCK_REGION = "us-east-1"
SERVICE_REGIONS = ["us-east-1", "us-west-2", "ap-south-1", "eu-west-1"]

# We price the standard synchronous on-demand path only. Batch / flex / cached
# dimensions are real but are not what a Converse call bills at, and silently
# mixing them in would understate cost.
_SKIP = re.compile(r"batch|flex|cache|priority", re.IGNORECASE)
_INPUT = re.compile(r"input[-_ ]tokens", re.IGNORECASE)
_OUTPUT = re.compile(r"output[-_ ]tokens", re.IGNORECASE)

# The synchronous, single-document operations the providers actually call.
# Custom-model, endpoint, training and async-job dimensions are excluded:
# they bill on a completely different basis.
_COMPREHEND_OPS = {
    "DetectPiiEntities", "DetectSentiment", "DetectDominantLanguage",
    "DetectEntities", "DetectKeyPhrases",
}
# Anchored on the hyphenated suffix, not bare words: 'DetectSentiment'
# contains the substring 'time', which a naive filter silently drops.
_COMPREHEND_EXCLUDE = re.compile(r"-Custom|-Storage|-Time|Endpoint",
                                 re.IGNORECASE)


def client():
    # The Price List API itself lives in a handful of regions only.
    return boto3.client("pricing", region_name="us-east-1")


def products(service_code: str, region: str):
    """Yield price records, preferring regionCode and falling back to location.

    Older services predate the regionCode attribute; newer ones do not always
    carry a location string that matches. Trying both is the only way to get
    a complete table without hardcoding a region-name map.
    """
    pag = client().get_paginator("get_products")
    for field, value in (("regionCode", region), ("location", _LOCATION.get(region, ""))):
        if not value:
            continue
        found = False
        for page in pag.paginate(ServiceCode=service_code,
                                 Filters=[{"Type": "TERM_MATCH",
                                           "Field": field, "Value": value}]):
            for blob in page["PriceList"]:
                found = True
                yield json.loads(blob)
        if found:
            return


_LOCATION = {
    "us-east-1": "US East (N. Virginia)",
    "us-west-2": "US West (Oregon)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "eu-west-1": "EU (Ireland)",
}


def _dimensions(rec):
    for term in rec.get("terms", {}).get("OnDemand", {}).values():
        for dim in term.get("priceDimensions", {}).values():
            yield dim


def model_key(usagetype: str) -> str:
    """'USE1-amazon.nova-lite-input-tokens' -> 'amazon.nova-lite'"""
    key = re.sub(r"^[A-Z0-9]+-", "", usagetype)
    key = re.sub(r"[-_](input|output)[-_ ]tokens.*$", "", key, flags=re.IGNORECASE)
    return key.strip("-_ ")


# ------------------------------------------------------------------ bedrock --

def fetch_bedrock() -> dict:
    prices: dict[str, dict] = {}
    scanned = 0
    for rec in products("AmazonBedrock", BEDROCK_REGION):
        scanned += 1
        attrs = rec.get("product", {}).get("attributes", {})
        usagetype = attrs.get("usagetype", "")
        if not usagetype or _SKIP.search(usagetype):
            continue
        is_in, is_out = _INPUT.search(usagetype), _OUTPUT.search(usagetype)
        if not (is_in or is_out):
            continue
        for dim in _dimensions(rec):
            if dim.get("unit") != "1K tokens":
                continue
            usd = float(dim.get("pricePerUnit", {}).get("USD", 0) or 0)
            if usd <= 0:
                continue
            entry = prices.setdefault(model_key(usagetype), {
                "model_name": attrs.get("model", ""),
                "provider": attrs.get("provider", ""),
            })
            # Price List quotes per 1K tokens; store per 1M - the unit every
            # model card and invoice actually uses.
            entry["input_per_1m" if is_in else "output_per_1m"] = round(usd * 1000, 6)

    # Keep partial entries too. The Price List API's Bedrock coverage is
    # uneven (some providers publish input tokens only), and a model with a
    # half-known price must render as "unknown", never as a guessed number.
    for v in prices.values():
        v["complete"] = "input_per_1m" in v and "output_per_1m" in v
    print(f"bedrock: scanned {scanned} records, "
          f"{sum(v['complete'] for v in prices.values())} fully priced models")
    return prices


# ------------------------------------------------------- managed services --

def _cheapest_first_tier(dims) -> float | None:
    """Take the tier that starts at zero usage.

    Comprehend is volume-tiered. The zero tier is what a normal workload
    actually pays, and quoting the 50M-requests-a-month tier would understate
    every estimate this tool prints.
    """
    best = None
    for dim in dims:
        if str(dim.get("beginRange", "0")) != "0":
            continue
        usd = float(dim.get("pricePerUnit", {}).get("USD", 0) or 0)
        if usd > 0 and (best is None or usd < best):
            best = usd
    return best


def fetch_services(region: str) -> dict:
    out: dict[str, dict] = {}

    for rec in products("comprehend", region):
        attrs = rec.get("product", {}).get("attributes", {})
        op, usagetype = attrs.get("operation", ""), attrs.get("usagetype", "")
        if op not in _COMPREHEND_OPS or _COMPREHEND_EXCLUDE.search(usagetype):
            continue
        usd = _cheapest_first_tier(_dimensions(rec))
        if usd:
            out[op] = {"per_unit": usd, "unit_chars": 100, "min_units": 3}

    for rec in products("translate", region):
        attrs = rec.get("product", {}).get("attributes", {})
        if attrs.get("operation") != "TranslateText":
            continue
        usd = _cheapest_first_tier(_dimensions(rec))
        if usd:
            out["TranslateText"] = {"per_char": usd}

    print(f"{region:<12} {len(out)} priced operations")
    return out


def main() -> int:
    models = fetch_bedrock()
    services = {r: fetch_services(r) for r in SERVICE_REGIONS}

    payload = {
        "_source": "AWS Price List API (pricing:GetProducts)",
        "_bedrock_region": BEDROCK_REGION,
        "_note": "Bedrock: USD per 1M tokens, on-demand synchronous. "
                 "Comprehend: USD per 100-char unit, 3-unit minimum. "
                 "Translate: USD per character.",
        "models": dict(sorted(models.items())),
        "services": services,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\nwrote {OUT}\n")

    for region, ops in services.items():
        for op, rec in sorted(ops.items()):
            rate = rec.get("per_unit") or rec.get("per_char")
            print(f"  {region:<12} {op:<24} ${rate:.8f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Turn a target plus a reply into real money.

Three billing models have to coexist:

  * Bedrock     - per 1M input tokens and per 1M output tokens.
  * Comprehend  - per 100-character unit, with a 3-unit floor per request.
                  That floor is why a one-line request costs the same as a
                  300-character one, and it dominates short-prompt workloads.
  * Translate   - per character, flat.

Prices come from the AWS Price List API (see scripts/fetch_pricing.py), whose
Bedrock usage-type names ('NovaLite', 'Nova2.0Lite') do not match model ids
('us.amazon.nova-lite-v1:0'). Normalise both to a comparable key.

If a target has no complete price, cost is None - never zero, never a guess.
Downstream, an unknown cost means "cannot compare", not "free".
"""
from __future__ import annotations

import json
import math
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "pricing.json")

_REGIONAL = ("us.", "global.", "eu.", "apac.")
_PROVIDERS = ("amazon.", "anthropic.", "meta.", "mistral.", "cohere.",
              "ai21.", "deepseek.", "qwen.", "openai.", "zai.", "nvidia.",
              "google.", "moonshot.", "stability.", "writer.", "twelvelabs.")

# Provider op -> the Price List operation that actually bills for it.
_COMPREHEND_OP = {
    "pii": "DetectPiiEntities",
    "redact": "DetectPiiEntities",
    "sentiment": "DetectSentiment",
    "language": "DetectDominantLanguage",
    "entities": "DetectEntities",
    "phrases": "DetectKeyPhrases",
}


def _norm(text: str) -> str:
    """Reduce any spelling of a model to a comparable token.

    'us.amazon.nova-2-lite-v1:0' -> 'nova2lite'
    'Nova2.0Lite'                -> 'nova2lite'
    """
    s = text.strip()
    for pre in _REGIONAL:
        if s.startswith(pre):
            s = s[len(pre):]
    for pre in _PROVIDERS:
        if s.startswith(pre):
            s = s[len(pre):]
    s = s.split(":")[0]
    s = re.sub(r"-\d{8}$", "", s)          # date stamps: -20240307
    s = re.sub(r"-v\d+$", "", s)           # version tails: -v1
    s = re.sub(r"\.0(?!\d)", "", s)        # Nova2.0Lite -> Nova2Lite
    s = re.sub(r"[-_\s.]", "", s)
    return s.lower()


with open(_PATH, encoding="utf-8") as _fh:
    _RAW = json.load(_fh)

SOURCE = _RAW.get("_source", "unknown")
_SERVICES: dict[str, dict] = _RAW.get("services", {})

_INDEX: dict[str, dict] = {}
for _key, _val in _RAW.get("models", {}).items():
    _INDEX.setdefault(_norm(_key), _val)
    # 'Nova2.0Pro-text' should also answer to 'nova2pro'.
    _stripped = re.sub(r"(text|speech)$", "", _norm(_key))
    if _stripped and _stripped != _norm(_key):
        _INDEX.setdefault(_stripped, _val)


def _service_rate(region: str, operation: str) -> dict | None:
    return _SERVICES.get(region, {}).get(operation)


def lookup_model(model_id: str) -> dict | None:
    """Return the price record for a Bedrock model id, or None if unpriced."""
    rec = _INDEX.get(_norm(model_id))
    return rec if rec and rec.get("complete") else None


def rate_card(raw_target: str) -> dict | None:
    """A human-readable rate for the dashboard, or None if we cannot price it.

    Deliberately returns the native unit rather than converting everything to
    a synthetic common unit - '$0.0001 per 100 chars' and '$0.30 per 1M input
    tokens' are not the same kind of number and pretending otherwise misleads.
    """
    from providers import parse  # local import: providers imports pricing-free

    try:
        target = parse(raw_target)
    except ValueError:
        return None

    if target.provider == "bedrock":
        rec = lookup_model(target.op)
        if not rec:
            return None
        return {"unit": "per 1M tokens",
                "input": rec["input_per_1m"], "output": rec["output_per_1m"]}

    if target.provider == "comprehend":
        rec = _service_rate(target.region, _COMPREHEND_OP.get(target.op, ""))
        if not rec:
            return None
        return {"unit": f"per {rec['unit_chars']} chars",
                "input": rec["per_unit"], "output": 0.0}

    if target.provider == "translate":
        rec = _service_rate(target.region, "TranslateText")
        if not rec:
            return None
        return {"unit": "per char", "input": rec["per_char"], "output": 0.0}

    return None


def cost_usd(raw_target: str, reply) -> float | None:
    """Cost of one call, in USD, or None if the target cannot be priced."""
    from providers import parse

    try:
        target = parse(raw_target)
    except ValueError:
        return None

    if target.provider == "bedrock":
        rec = lookup_model(target.op)
        if not rec:
            return None
        return (reply.in_tokens / 1_000_000) * rec["input_per_1m"] + \
               (reply.out_tokens / 1_000_000) * rec["output_per_1m"]

    if target.provider == "comprehend":
        rec = _service_rate(target.region, _COMPREHEND_OP.get(target.op, ""))
        if not rec:
            return None
        units = max(rec["min_units"],
                    int(math.ceil(reply.chars / rec["unit_chars"])))
        return units * rec["per_unit"]

    if target.provider == "translate":
        rec = _service_rate(target.region, "TranslateText")
        if not rec:
            return None
        return reply.chars * rec["per_char"]

    return None

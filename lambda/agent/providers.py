"""Target registry: one call interface over several AWS AI services.

A *target* is the string an engineer writes in a contract:

    bedrock:us.amazon.nova-lite-v1:0        foundation model, via Converse
    comprehend:redact@ap-south-1            managed PII redaction, in Mumbai
    comprehend:pii?min=0.99                 same service, stricter threshold
    translate:fr?back=en                    translate out and back again

Grammar:  provider ":" op [ "@" region ] [ "?" k=v & k=v ]

Every provider normalises its answer to TEXT, emitting JSON wherever the
service returns structure. That is the whole trick: the assertion library
never learns that providers exist, so a contract written against a foundation
model can be pointed at a managed service without changing a line of it - and
the two can be scored side by side in the same run.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field

from botocore.exceptions import ClientError

import bedrock
from reply import Reply

DEFAULT_REGION = "us-east-1"

# Comprehend bills in 100-character units with a 3-unit floor per request.
COMPREHEND_UNIT_CHARS = 100
COMPREHEND_MIN_UNITS = 3


@dataclass(frozen=True)
class Target:
    raw: str
    provider: str
    op: str
    region: str = DEFAULT_REGION
    params: dict = field(default_factory=dict)

    @property
    def short(self) -> str:
        """A label a human can read in a table."""
        name = self.op.replace("us.", "").replace("amazon.", "")
        name = name.replace("-v1:0", "")
        extra = "".join(f" {k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.provider}:{name}@{self.region}{extra}"


def parse(raw: str) -> Target:
    rest, params = raw.strip(), {}
    if "?" in rest:
        rest, query = rest.split("?", 1)
        for pair in query.split("&"):
            if "=" in pair:
                key, value = pair.split("=", 1)
                params[key.strip()] = value.strip()

    region = DEFAULT_REGION
    if "@" in rest:
        rest, region = rest.rsplit("@", 1)

    if ":" not in rest:
        raise ValueError(f"target '{raw}' is missing a provider prefix")
    # Split once only: a Bedrock model id has its own colon ('...-v1:0').
    provider, op = rest.split(":", 1)
    return Target(raw, provider.strip().lower(), op.strip(), region.strip(), params)


def _num(params: dict, key: str, default: float) -> float:
    try:
        return float(params.get(key, default))
    except (TypeError, ValueError):
        return default


def comprehend_units(chars: int) -> int:
    return max(COMPREHEND_MIN_UNITS,
               int(math.ceil(chars / COMPREHEND_UNIT_CHARS)))


# ------------------------------------------------------------- comprehend --

def _redact(text: str, entities: list[dict], marker: str) -> str:
    """Splice the marker over every detected span.

    Right to left, because replacing left to right invalidates every offset
    after the first substitution.
    """
    out = text
    for ent in sorted(entities, key=lambda e: e["BeginOffset"], reverse=True):
        out = out[:ent["BeginOffset"]] + marker + out[ent["EndOffset"]:]
    return out


def _comprehend(target: Target, text: str) -> Reply:
    op = target.op
    lang = target.params.get("lang", "en")
    floor = _num(target.params, "min", 0.5)
    marker = target.params.get("marker", "[REDACTED]")
    client = bedrock.client("comprehend", target.region)

    started = time.perf_counter()
    if op in ("pii", "redact"):
        found = client.detect_pii_entities(Text=text, LanguageCode=lang)["Entities"]
        kept = [e for e in found if e.get("Score", 0) >= floor]
        if op == "redact":
            # Plain text on purpose: this is the same shape a redaction prompt
            # returns from a foundation model, so one contract scores both.
            body = _redact(text, kept, marker)
        else:
            body = json.dumps({
                "types": sorted({e["Type"] for e in kept}),
                "count": len(kept),
                "entities": [
                    {"type": e["Type"],
                     "text": text[e["BeginOffset"]:e["EndOffset"]],
                     "score": round(e.get("Score", 0), 4)}
                    for e in kept
                ],
            })
    elif op == "sentiment":
        resp = client.detect_sentiment(Text=text, LanguageCode=lang)
        body = json.dumps({
            "sentiment": resp["Sentiment"],
            "scores": {k: round(v, 4)
                       for k, v in resp.get("SentimentScore", {}).items()},
        })
    elif op == "language":
        langs = client.detect_dominant_language(Text=text)["Languages"]
        best = max(langs, key=lambda l: l.get("Score", 0)) if langs else {}
        body = json.dumps({
            "language": best.get("LanguageCode", ""),
            "score": round(best.get("Score", 0), 4),
        })
    elif op == "entities":
        found = client.detect_entities(Text=text, LanguageCode=lang)["Entities"]
        kept = [e for e in found if e.get("Score", 0) >= floor]
        body = json.dumps({
            "types": sorted({e["Type"] for e in kept}),
            "count": len(kept),
            "entities": [{"type": e["Type"], "text": e["Text"],
                          "score": round(e.get("Score", 0), 4)} for e in kept],
        })
    elif op == "phrases":
        found = client.detect_key_phrases(Text=text, LanguageCode=lang)["KeyPhrases"]
        kept = [p for p in found if p.get("Score", 0) >= floor]
        body = json.dumps({"count": len(kept),
                           "phrases": [p["Text"] for p in kept]})
    else:
        return Reply(ok=False, error_code="UnknownOp",
                     error=f"comprehend has no op '{op}'")

    return Reply(ok=True, text=body, chars=len(text),
                 latency_ms=int((time.perf_counter() - started) * 1000))


# -------------------------------------------------------------- translate --

def _translate(target: Target, text: str) -> Reply:
    client = bedrock.client("translate", target.region)
    source = target.params.get("src", "auto")
    back = target.params.get("back", "")

    started = time.perf_counter()
    out = client.translate_text(Text=text, SourceLanguageCode=source,
                                TargetLanguageCode=target.op)
    translated = out["TranslatedText"]
    billed = len(text)

    if back:
        # Round-tripping is the only mechanical way to say anything about
        # translation fidelity without a second human language in the loop.
        again = client.translate_text(
            Text=translated,
            SourceLanguageCode=out.get("TargetLanguageCode", target.op),
            TargetLanguageCode=back)
        billed += len(translated)
        body = json.dumps({"translated": translated,
                           "roundtrip": again["TranslatedText"],
                           "detected_source": out.get("SourceLanguageCode", source)})
    else:
        body = translated

    return Reply(ok=True, text=body, chars=billed,
                 latency_ms=int((time.perf_counter() - started) * 1000))


# ----------------------------------------------------------------- bedrock --

def _bedrock(target: Target, text: str, contract: dict) -> Reply:
    return bedrock.converse(
        target.op, text,
        system=contract.get("system", ""),
        max_tokens=contract.get("max_tokens", 512),
        # Temperature 0 for reproducibility. Repetitions still vary - which is
        # precisely the finding worth surfacing.
        temperature=contract.get("temperature", 0.0),
        region=target.region,
    )


# --------------------------------------------------------------- dispatch --

_HANDLERS = {"bedrock", "comprehend", "translate"}


def invoke(raw_target: str, text: str, contract: dict | None = None) -> Reply:
    contract = contract or {}
    try:
        target = parse(raw_target)
    except ValueError as exc:
        return Reply(ok=False, error_code="BadTarget", error=str(exc))

    if target.provider not in _HANDLERS:
        return Reply(ok=False, error_code="UnknownProvider",
                     error=f"no provider '{target.provider}'")

    try:
        if target.provider == "bedrock":
            return _bedrock(target, text, contract)
        if target.provider == "comprehend":
            return _comprehend(target, text)
        return _translate(target, text)
    except ClientError as exc:
        return Reply(ok=False,
                     error_code=exc.response.get("Error", {}).get("Code", "ClientError"),
                     error=str(exc))
    except Exception as exc:  # noqa: BLE001 - one bad target must not end a run
        return Reply(ok=False, error_code=type(exc).__name__, error=str(exc))


_BOTO_NOISE = re.compile(
    r"^An error occurred \([^)]+\) when calling the \w+ operation:\s*")


def clean_error(message: str) -> str:
    """Botocore prefixes the useful sentence with 35 characters of ceremony.

    That prefix is exactly what survives truncation in a dashboard cell or an
    SMS alert, leaving the reader with no idea what went wrong.
    """
    return _BOTO_NOISE.sub("", message or "").strip() or "unknown error"


def probe(raw_target: str) -> tuple[bool, str]:
    """Cheapest call that settles 'can this account use this right now?'."""
    sample = {"translate": "Hello", "comprehend": "Call Bob at 555-0142."}
    try:
        target = parse(raw_target)
    except ValueError as exc:
        return False, str(exc)

    text = sample.get(target.provider, "Reply with the single word: OK")
    if target.provider == "bedrock":
        reply = bedrock.converse(target.op, text, max_tokens=5, attempts=1,
                                 region=target.region)
    else:
        reply = invoke(raw_target, text)

    if reply.ok:
        return True, f"OK ({reply.latency_ms}ms)"
    return False, f"{reply.error_code}: {clean_error(reply.error)[:110]}"

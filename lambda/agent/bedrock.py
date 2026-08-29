"""Thin Converse wrapper: uniform result shape, honest errors, backoff."""
from __future__ import annotations

import random
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from reply import Reply

DEFAULT_REGION = "us-east-1"

_clients: dict[tuple[str, str], object] = {}


def _cfg(region: str) -> Config:
    # Bedrock throttles hard when you fan out across models. Let botocore retry
    # the transient classes itself, then add our own jittered backoff on top for
    # the ones it gives up on.
    return Config(
        region_name=region,
        retries={"max_attempts": 3, "mode": "adaptive"},
        read_timeout=120,
        connect_timeout=10,
    )


def client(service: str, region: str = DEFAULT_REGION):
    key = (service, region)
    if key not in _clients:
        _clients[key] = boto3.client(service, config=_cfg(region))
    return _clients[key]


def runtime(region: str = DEFAULT_REGION):
    return client("bedrock-runtime", region)


def control(region: str = DEFAULT_REGION):
    return client("bedrock", region)


# Retrying these is pointless - the answer will not change.
_FATAL = {
    "ValidationException", "AccessDeniedException",
    "ResourceNotFoundException", "UnrecognizedClientException",
}


def converse(model_id: str, user_text: str, system: str = "",
             max_tokens: int = 512, temperature: float = 0.0,
             attempts: int = 4, region: str = DEFAULT_REGION) -> Reply:
    kwargs = {
        "modelId": model_id,
        "messages": [{"role": "user", "content": [{"text": user_text}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
    }
    if system:
        kwargs["system"] = [{"text": system}]

    last = ""
    last_code = ""
    for attempt in range(attempts):
        started = time.perf_counter()
        try:
            resp = runtime(region).converse(**kwargs)
            wall_ms = int((time.perf_counter() - started) * 1000)
            usage = resp.get("usage", {})
            return Reply(
                ok=True,
                text=resp["output"]["message"]["content"][0].get("text", ""),
                in_tokens=usage.get("inputTokens", 0),
                out_tokens=usage.get("outputTokens", 0),
                chars=len(user_text),
                # Prefer Bedrock's own number; fall back to wall clock.
                latency_ms=resp.get("metrics", {}).get("latencyMs", wall_ms),
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            last, last_code = str(exc), code
            if code in _FATAL:
                break
            if attempt < attempts - 1:
                time.sleep(min(2 ** attempt + random.random(), 12))
        except Exception as exc:  # noqa: BLE001 - never kill a whole run
            last, last_code = str(exc), type(exc).__name__
            if attempt < attempts - 1:
                time.sleep(min(2 ** attempt + random.random(), 12))

    return Reply(ok=False, error=last, error_code=last_code)

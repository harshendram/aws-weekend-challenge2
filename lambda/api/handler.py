"""Public read-only API + dashboard, served from one Lambda Function URL.

Serving the HTML from the same Lambda that serves the JSON means one origin,
no CORS, no public S3 bucket, and no CloudFront propagation wait.

Everything here is readable by anyone. The single write path - triggering a
run - requires a shared secret, so a public URL can never spend AWS money.
"""
from __future__ import annotations

import json
import os
from urllib.parse import unquote

import boto3
from boto3.dynamodb.conditions import Key

TABLE = os.environ.get("RADAR_TABLE", "driftradar")
BUCKET = os.environ.get("RADAR_BUCKET", "")
AGENT_FN = os.environ.get("RADAR_AGENT_FN", "radar-agent")
RUN_KEY = os.environ.get("RADAR_RUN_KEY", "")
REGION = os.environ.get("RADAR_REGION", "us-east-1")

HERE = os.path.dirname(os.path.abspath(__file__))

_ddb = _s3 = _lam = None


def table():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    return _ddb


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=REGION)
    return _s3


def lam():
    global _lam
    if _lam is None:
        _lam = boto3.client("lambda", region_name=REGION)
    return _lam


def reply(status: int, body, content_type="application/json"):
    return {
        "statusCode": status,
        "headers": {
            "content-type": content_type,
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        },
        "body": body if isinstance(body, str) else json.dumps(body, default=str),
    }


def query(pk: str, limit: int, forward: bool = False):
    return table().query(KeyConditionExpression=Key("pk").eq(pk),
                         ScanIndexForward=forward, Limit=limit).get("Items", [])


def list_runs(limit=40):
    out = []
    for item in query("RUN", limit):
        run = json.loads(item["payload"])
        run.pop("scores", None)  # the listing stays light
        out.append(run)
    return out


def get_run(run_id):
    item = table().get_item(Key={"pk": "RUN", "sk": run_id}).get("Item")
    return json.loads(item["payload"]) if item else None


def latest_snapshot():
    item = table().get_item(Key={"pk": "SNAPSHOT", "sk": "LATEST"}).get("Item")
    if not item:
        return None
    snap = json.loads(item["payload"])
    # The per-region model id lists are large and the dashboard only ever
    # renders their length, so they are summarised rather than shipped.
    snap["regions"] = {
        region: {"on_demand": len(cat.get("on_demand", [])),
                 "profiles": len(cat.get("profiles", [])),
                 "error": cat.get("error", "")}
        for region, cat in snap.get("regions", {}).items()
    }
    return snap


def list_events(limit=40):
    out = []
    for item in query("EVENT", limit):
        event = json.loads(item["payload"])
        event["checked_at"] = item.get("checked_at", "")
        out.append(event)
    return out


def lambda_handler(event, context):  # noqa: ARG001
    ctx = (event or {}).get("requestContext", {}).get("http", {})
    method = ctx.get("method", "GET")
    path = (event or {}).get("rawPath", "/") or "/"
    qs = (event or {}).get("queryStringParameters") or {}
    headers = {k.lower(): v for k, v in ((event or {}).get("headers") or {}).items()}

    try:
        if path in ("/", ""):
            with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as fh:
                return reply(200, fh.read(), "text/html; charset=utf-8")

        if path == "/api/health":
            return reply(200, {"ok": True, "table": TABLE, "bucket": bool(BUCKET)})

        if path == "/api/runs" and method == "GET":
            return reply(200, {"runs": list_runs(int(qs.get("limit", 40)))})

        if path.startswith("/api/runs/") and method == "GET":
            run = get_run(unquote(path[len("/api/runs/"):]))
            return reply(200, run) if run else reply(404, {"error": "no such run"})

        if path == "/api/reach" and method == "GET":
            snap = latest_snapshot()
            return reply(200, snap) if snap else reply(
                404, {"error": "no scan recorded yet"})

        if path == "/api/events" and method == "GET":
            return reply(200, {"events": list_events(int(qs.get("limit", 40)))})

        if path == "/api/attempts" and method == "GET":
            run_id, target = qs.get("run", ""), qs.get("target", "")
            if not (run_id and target and BUCKET):
                return reply(400, {"error": "run and target required"})
            import re
            key = f"runs/{run_id}/{re.sub(r'[^A-Za-z0-9._-]', '_', target)}.json"
            try:
                obj = s3().get_object(Bucket=BUCKET, Key=key)
                return reply(200, obj["Body"].read().decode())
            except Exception:
                return reply(404, {"error": "no stored output for that target"})

        if path == "/api/run" and method == "POST":
            # The only write path in the whole application.
            if not RUN_KEY or headers.get("x-radar-key") != RUN_KEY:
                return reply(401, {"error": "unauthorized"})
            body = json.loads(event.get("body") or "{}")
            payload = {"mode": body.get("mode", "run"), "trigger": "dashboard"}
            for field in ("contract", "targets"):
                if body.get(field):
                    payload[field] = body[field]
            # Fire and forget: the dashboard polls DynamoDB for the result, so
            # a slow multi-target run can never time out an HTTP request.
            lam().invoke(FunctionName=AGENT_FN, InvocationType="Event",
                         Payload=json.dumps(payload).encode())
            return reply(202, {"accepted": True, "payload": payload})

        return reply(404, {"error": f"no route for {method} {path}"})
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return reply(500, {"error": f"{type(exc).__name__}: {exc}"})

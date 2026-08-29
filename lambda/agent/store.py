"""DynamoDB + S3 persistence.

Summaries live in DynamoDB as a JSON string attribute rather than a nested
map. DynamoDB rejects floats and would force Decimal conversion through every
nested structure; serialising once keeps the scoring code free of storage
concerns. Raw per-attempt output goes to S3, well clear of the 400KB item cap.

One table, three item kinds, discriminated by partition key:
    RUN       one contract execution
    SNAPSHOT  one reachability scan
    EVENT     one detected change, kept so the timeline survives the next scan
"""
from __future__ import annotations

import json
import os
import re
import time

import boto3
from boto3.dynamodb.conditions import Key

TABLE = os.environ.get("RADAR_TABLE", "driftradar")
BUCKET = os.environ.get("RADAR_BUCKET", "")
REGION = os.environ.get("RADAR_REGION", "us-east-1")
TTL_DAYS = int(os.environ.get("RADAR_TTL_DAYS", "30"))

_ddb = None
_s3 = None


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


def _ttl(days: int = TTL_DAYS) -> int:
    return int(time.time()) + days * 86400


def safe_key(target: str) -> str:
    """A target id is a URL-ish string; an S3 key should not be."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", target)


# ------------------------------------------------------------------- runs --

def save_run(run: dict) -> str:
    """Persist one run. Returns the run id."""
    run_id = run["run_id"]
    heavy = {}
    for score in run.get("scores", []):
        heavy[score["target"]] = score.pop("attempts", [])

    if BUCKET:
        for target, attempts in heavy.items():
            key = f"runs/{run_id}/{safe_key(target)}.json"
            try:
                s3().put_object(
                    Bucket=BUCKET, Key=key,
                    Body=json.dumps(attempts, indent=2).encode(),
                    ContentType="application/json")
            except Exception as exc:  # noqa: BLE001 - never lose the summary
                print(f"[store] S3 write failed for {target}: {exc}")

    table().put_item(Item={
        "pk": "RUN",
        "sk": run_id,
        "run_id": run_id,
        "contract": run["contract"],
        "created_at": run["created_at"],
        "trigger": run.get("trigger", "manual"),
        "payload": json.dumps(run),
        "expires_at": _ttl(),
    })
    return run_id


def list_runs(limit: int = 40) -> list[dict]:
    resp = table().query(
        KeyConditionExpression=Key("pk").eq("RUN"),
        ScanIndexForward=False, Limit=limit,
    )
    out = []
    for item in resp.get("Items", []):
        run = json.loads(item["payload"])
        run.pop("scores", None)  # summary listing stays light
        out.append(run)
    return out


def get_run(run_id: str) -> dict | None:
    resp = table().get_item(Key={"pk": "RUN", "sk": run_id})
    item = resp.get("Item")
    return json.loads(item["payload"]) if item else None


def get_attempts(run_id: str, target: str) -> list | None:
    if not BUCKET:
        return None
    try:
        obj = s3().get_object(Bucket=BUCKET,
                              Key=f"runs/{run_id}/{safe_key(target)}.json")
        return json.loads(obj["Body"].read())
    except Exception:
        return None


# -------------------------------------------------------------- snapshots --

def save_snapshot(snap: dict) -> None:
    """Two writes on purpose.

    LATEST is what the next scan diffs against and must be a single
    predictable key; the timestamped copy is what makes the history
    reconstructable when someone asks "when did this break?".
    """
    payload = json.dumps(snap)
    table().put_item(Item={"pk": "SNAPSHOT", "sk": "LATEST",
                           "checked_at": snap["checked_at"], "payload": payload})
    table().put_item(Item={"pk": "SNAPSHOT", "sk": snap["checked_at"],
                           "checked_at": snap["checked_at"], "payload": payload,
                           "expires_at": _ttl(90)})


def latest_snapshot() -> dict | None:
    item = table().get_item(Key={"pk": "SNAPSHOT", "sk": "LATEST"}).get("Item")
    return json.loads(item["payload"]) if item else None


def list_snapshots(limit: int = 30) -> list[dict]:
    resp = table().query(
        KeyConditionExpression=Key("pk").eq("SNAPSHOT"),
        ScanIndexForward=False, Limit=limit + 1)
    return [json.loads(i["payload"]) for i in resp.get("Items", [])
            if i["sk"] != "LATEST"][:limit]


# ----------------------------------------------------------------- events --

def save_events(events: list[dict], checked_at: str) -> None:
    for n, event in enumerate(events):
        table().put_item(Item={
            "pk": "EVENT",
            "sk": f"{checked_at}#{n:03d}",
            "checked_at": checked_at,
            "payload": json.dumps(event),
            "expires_at": _ttl(90),
        })


def list_events(limit: int = 50) -> list[dict]:
    resp = table().query(
        KeyConditionExpression=Key("pk").eq("EVENT"),
        ScanIndexForward=False, Limit=limit)
    out = []
    for item in resp.get("Items", []):
        event = json.loads(item["payload"])
        event["checked_at"] = item["checked_at"]
        out.append(event)
    return out

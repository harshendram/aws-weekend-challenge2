"""Build Lambda deployment zips. Pure stdlib so it runs anywhere.

boto3 ships in the Lambda Python runtime, so there is nothing to vendor -
these zips are just source.
"""
from __future__ import annotations

import os
import pathlib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"


def add(zf: zipfile.ZipFile, src: pathlib.Path, arc: str) -> None:
    zf.write(src, arc)
    print(f"   + {arc}")


def build_agent() -> pathlib.Path:
    out = BUILD / "agent.zip"
    print(f"\nagent.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for py in sorted((ROOT / "lambda" / "agent").glob("*.py")):
            add(zf, py, py.name)
        add(zf, ROOT / "lambda" / "agent" / "pricing.json", "pricing.json")
        # Contracts travel with the function so a run needs no external fetch.
        for c in sorted((ROOT / "contracts").glob("*.json")):
            add(zf, c, f"contracts/{c.name}")
    return out


def build_api() -> pathlib.Path:
    out = BUILD / "api.zip"
    print(f"\napi.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        add(zf, ROOT / "lambda" / "api" / "handler.py", "handler.py")
        add(zf, ROOT / "lambda" / "api" / "dashboard.html", "dashboard.html")
    return out


def main() -> int:
    BUILD.mkdir(exist_ok=True)
    for path in (build_agent(), build_api()):
        print(f"   -> {path}  ({path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

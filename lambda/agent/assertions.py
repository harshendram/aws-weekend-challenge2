"""Assertion library for behaviour contracts.

Two families:
  * deterministic - pure functions over the model's raw output. Cheap, fast,
    reproducible. These decide the score.
  * judged        - delegated to a cheap Bedrock model, ONLY for properties that
    cannot be checked mechanically (refusal, groundedness, language).

Every assertion returns an Outcome so a failure can explain itself in the
dashboard without re-running anything.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Any


DETERMINISTIC = {
    "json_valid", "json_has_path", "json_path_in", "no_markdown_fence",
    "max_output_tokens", "contains_any", "not_contains", "regex_match",
    "max_chars", "is_one_of",
    # List- and number-shaped assertions, for services that answer with
    # structure rather than prose.
    "json_path_includes", "json_path_excludes", "json_len_between",
    "json_num_at_least",
}
JUDGED = {"refuses", "grounded_in_context", "language_is"}

_FENCE_RE = re.compile(r"```")


@dataclass
class Outcome:
    type: str
    passed: bool
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- parsing --

def strict_parse(text: str):
    """Parse with no forgiveness. Used by json_valid: a fenced or chatty
    response is exactly the failure we want to catch."""
    return json.loads(text)


def lenient_parse(text: str):
    """Best-effort parse so a *field* assertion can still report on a response
    that merely arrived wrapped in a fence. Returns None if unrecoverable.

    This split matters: without it, one malformed response fails every
    assertion at once and the scorecard cannot tell you *what* went wrong.
    """
    try:
        return json.loads(text)
    except Exception:
        pass
    stripped = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(),
                      flags=re.IGNORECASE)
    try:
        return json.loads(stripped)
    except Exception:
        pass
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(stripped[start:end + 1])
        except Exception:
            return None
    return None


def dig(obj: Any, path: str):
    """Walk a dotted path. Supports list indices: 'items.0.name'.
    Returns the sentinel _MISSING when any hop fails."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            if not part.isdigit() or int(part) >= len(cur):
                return _MISSING
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            if part not in cur:
                return _MISSING
            cur = cur[part]
        else:
            return _MISSING
    return cur


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


def _clip(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\n", "\n")
    return text if len(text) <= limit else text[:limit] + "..."


# --------------------------------------------------------- deterministic --

def check_deterministic(spec: dict, output: str, out_tokens: int) -> Outcome:
    kind = spec["type"]

    if kind == "json_valid":
        try:
            strict_parse(output)
            return Outcome(kind, True, "parsed as JSON")
        except Exception as exc:
            return Outcome(kind, False, f"not valid JSON: {exc}")

    if kind in ("json_has_path", "json_path_in"):
        data = lenient_parse(output)
        if data is None:
            return Outcome(kind, False, "response was not JSON at all")
        found = dig(data, spec["path"])
        if found is _MISSING:
            return Outcome(kind, False, f"path '{spec['path']}' missing")
        if kind == "json_has_path":
            return Outcome(kind, True, f"{spec['path']}={_clip(found)}")
        allowed = spec["values"]
        ok = found in allowed
        return Outcome(kind, ok,
                       f"{spec['path']}={_clip(found)}"
                       + ("" if ok else f" not in {allowed}"))

    if kind in ("json_path_includes", "json_path_excludes",
                "json_len_between", "json_num_at_least"):
        data = lenient_parse(output)
        if data is None:
            return Outcome(kind, False, "response was not JSON at all")
        found = dig(data, spec["path"])
        if found is _MISSING:
            return Outcome(kind, False, f"path '{spec['path']}' missing")

        if kind == "json_num_at_least":
            if not isinstance(found, (int, float)):
                return Outcome(kind, False, f"{spec['path']} is not a number")
            ok = found >= spec["value"]
            return Outcome(kind, ok, f"{spec['path']}={found} "
                                     f"(need >= {spec['value']})")

        if not isinstance(found, list):
            return Outcome(kind, False, f"{spec['path']} is not a list")

        if kind == "json_len_between":
            low, high = spec.get("min", 0), spec.get("max", 10 ** 9)
            ok = low <= len(found) <= high
            return Outcome(kind, ok, f"{len(found)} item(s), want {low}-{high}")

        wanted = set(spec["values"])
        present = wanted & set(map(str, found))
        if kind == "json_path_includes":
            missing = sorted(wanted - present)
            return Outcome(kind, not missing,
                           "all present" if not missing else f"missing {missing}")
        # json_path_excludes - report every offender, not just the first.
        return Outcome(kind, not present,
                       "clean" if not present else f"unwanted {sorted(present)}")

    if kind == "no_markdown_fence":
        ok = not _FENCE_RE.search(output)
        return Outcome(kind, ok, "clean" if ok else "output contains ``` fence")

    if kind == "max_output_tokens":
        ok = out_tokens <= spec["value"]
        return Outcome(kind, ok, f"{out_tokens} tokens (limit {spec['value']})")

    if kind == "max_chars":
        ok = len(output) <= spec["value"]
        return Outcome(kind, ok, f"{len(output)} chars (limit {spec['value']})")

    if kind == "contains_any":
        hits = [n for n in spec["values"] if n.lower() in output.lower()]
        return Outcome(kind, bool(hits),
                       f"found {hits}" if hits else f"none of {spec['values']}")

    if kind == "not_contains":
        # The critical one for redaction: report every leak, not just the first.
        leaks = [n for n in spec["values"] if n.lower() in output.lower()]
        return Outcome(kind, not leaks,
                       "clean" if not leaks else f"LEAKED {leaks}")

    if kind == "regex_match":
        ok = re.search(spec["pattern"], output, flags=re.IGNORECASE | re.DOTALL) is not None
        return Outcome(kind, ok, f"/{spec['pattern']}/ "
                                 + ("matched" if ok else "did not match"))

    if kind == "is_one_of":
        ok = output.strip() in spec["values"]
        return Outcome(kind, ok, f"got {_clip(output.strip())}")

    return Outcome(kind, False, f"unknown assertion type '{kind}'")


# ---------------------------------------------------------------- judged --

_JUDGE_PROMPTS = {
    "refuses": (
        "Did the assistant DECLINE to answer, or state that the provided "
        "material does not contain the answer? Answer PASS if it refused or "
        "said it did not know. Answer FAIL if it supplied a substantive answer."
    ),
    "grounded_in_context": (
        "Every factual claim in the assistant's answer must be supported by "
        "the CONTEXT. Answer PASS if fully supported. Answer FAIL if it "
        "asserts any fact absent from the context."
    ),
    "language_is": (
        "Answer PASS if the assistant's reply is written predominantly in the "
        "language with ISO code '{lang}'. Otherwise answer FAIL."
    ),
}


def judge_prompt(spec: dict, case: dict, output: str) -> str:
    instruction = _JUDGE_PROMPTS[spec["type"]].format(lang=spec.get("value", ""))
    context = case.get("context", "")
    return (
        "You are a strict evaluator. Reply with exactly one word: PASS or FAIL.\n\n"
        f"CRITERION:\n{instruction}\n\n"
        + (f"CONTEXT:\n{context}\n\n" if context else "")
        + f"USER ASKED:\n{case['input']}\n\nASSISTANT REPLIED:\n{output}\n\n"
        "One word, PASS or FAIL:"
    )


def parse_judge(verdict_text: str, kind: str) -> Outcome:
    token = verdict_text.strip().upper()
    if "PASS" in token:
        return Outcome(kind, True, "judge: PASS")
    if "FAIL" in token:
        return Outcome(kind, False, "judge: FAIL")
    # Never silently treat an unparseable judge reply as a pass.
    return Outcome(kind, False, f"judge returned unparseable '{_clip(verdict_text, 40)}'")

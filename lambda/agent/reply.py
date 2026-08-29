"""The one result shape every provider returns.

Kept in its own module so the transport layers (Bedrock, Comprehend,
Translate) and the scoring engine can all agree on it without importing
each other.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Reply:
    """One call against one target.

    `ok=False` means the call itself failed. That is reported distinctly from
    "the service answered but broke the contract" - conflating the two is how
    evaluation harnesses end up lying to you.

    Billing is recorded in whatever unit the service actually charges in:
    Bedrock bills tokens, Comprehend bills 100-character units, Translate
    bills characters. Normalising these into a fake common unit would bake in
    a conversion nobody asked for, so each is carried as-is and priced later.
    """
    ok: bool
    text: str = ""
    in_tokens: int = 0
    out_tokens: int = 0
    chars: int = 0
    latency_ms: int = 0
    error: str = ""
    error_code: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "text": self.text, "in_tokens": self.in_tokens,
            "out_tokens": self.out_tokens, "chars": self.chars,
            "latency_ms": self.latency_ms, "error": self.error,
            "error_code": self.error_code,
        }

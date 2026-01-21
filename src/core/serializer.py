"""Canonical serialization for deterministic signing payloads."""

from __future__ import annotations

import json
from typing import Any

from eth_utils.crypto import keccak


def _validate_for_serialization(obj: Any) -> None:
    if isinstance(obj, float):
        raise ValueError("Floating point values are not allowed")

    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError("All dictionary keys must be strings")
            _validate_for_serialization(value)
        return

    if isinstance(obj, list):
        for item in obj:
            _validate_for_serialization(item)
        return

    if obj is None or isinstance(obj, (str, int, bool)):
        return

    raise TypeError(f"Unsupported type for serialization: {type(obj).__name__}")


class CanonicalSerializer:
    """
    Produces deterministic JSON for signing.

    Rules:
    - Keys sorted alphabetically (recursive)
    - No whitespace
    - Numbers as-is (but prefer string amounts in trading data)
    - Consistent unicode handling
    """

    @staticmethod
    def serialize(obj: Any) -> bytes:
        """Returns canonical bytes representation."""
        _validate_for_serialization(obj)
        payload = json.dumps(
            obj,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return payload.encode("utf-8")

    @staticmethod
    def hash(obj: Any) -> bytes:
        """Returns keccak256 of canonical serialization."""
        return keccak(CanonicalSerializer.serialize(obj))

    @staticmethod
    def verify_determinism(obj: Any, iterations: int = 100) -> bool:
        """Verifies serialization is deterministic over N iterations."""
        if iterations < 1:
            raise ValueError("iterations must be >= 1")
        baseline = CanonicalSerializer.serialize(obj)
        for _ in range(iterations - 1):
            if CanonicalSerializer.serialize(obj) != baseline:
                return False
        return True

"""Canonical serialization for the verdict document and its evidence.

The report and every evidence file are serialized the same way the live
snapshot is: sorted keys, compact separators, ASCII, and **no floats
anywhere** — two serializations of the same content are identical bytes,
which is what makes the sha256 references and the Ed25519 signature
meaningful.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from enum import StrEnum

type JsonValue = bool | int | str | list["JsonValue"] | dict[str, "JsonValue"] | None


def jsonable(value: object) -> JsonValue:
    """Reduce to plain JSON types, rejecting anything nondeterministic.

    Frozensets are sorted (their iteration order is arbitrary), StrEnums
    become their machine value, dataclasses become field dicts. Floats are
    banned outright, exactly as in the snapshot serializer: a float would
    make the canonical form platform- and precision-dependent.
    """
    if isinstance(value, float):
        raise TypeError(f"floats are banned from reports; got {value!r}")
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, str):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, frozenset | set):
        items: list[JsonValue] = []
        for item in value:
            normalized = jsonable(item)
            if not isinstance(normalized, str):
                raise TypeError(
                    f"only sets of strings serialize deterministically: {value!r}"
                )
            items.append(normalized)
        items.sort(key=str)
        return items
    if isinstance(value, tuple | list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        out: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"non-string dict key {key!r} in report")
            out[key] = jsonable(item)
        return out
    raise TypeError(f"unserializable value in report: {value!r} ({type(value).__name__})")


def canonical_json(value: object) -> str:
    """Stable serialization: two calls on equal content yield equal bytes."""
    return json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

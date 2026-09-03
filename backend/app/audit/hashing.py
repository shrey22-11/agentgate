"""
Pure, deterministic hashing for the audit chain. No I/O, no ORM, no LLM.

Public surface:
    GENESIS_PREV_HASH        the `prev_hash` of the first event
    to_json_safe(value)      Decimal/datetime/UUID-aware, float-rejecting normaliser
    canonical_bytes(obj)     obj -> deterministic UTF-8 bytes
    compute_event_hash(...)  the hash contract (see docs/audit.md)
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

# 64 hex zeros: obviously a sentinel, still a valid String(64) value.
GENESIS_PREV_HASH = "0" * 64

# Keys of these shapes are how non-JSON scalars are represented inside payloads.
_DECIMAL_TAG = "__decimal__"
_DATETIME_TAG = "__datetime__"
_DATE_TAG = "__date__"
_UUID_TAG = "__uuid__"


def _decimal_to_canonical_str(value: Decimal) -> str:
    """
    `Decimal("9000.00")` and `Decimal("9000")` both -> "9000";
    `Decimal("9000.50")` and `Decimal("9000.5")` both -> "9000.5".

    Rationale: the audit reader cares about the *value*, not the scale, and
    normalising avoids a class of false tamper-positives when a value is
    re-quantised somewhere upstream. Non-finite Decimals are rejected.
    """
    if not value.is_finite():
        raise ValueError("audit payloads must not contain non-finite Decimal")
    normalised = value.normalize()
    if normalised == 0:
        return "0"  # collapses Decimal('0'), Decimal('-0'), Decimal('0.00')
    return format(normalised, "f")  # plain notation, never "9E+3"


def _datetime_to_canonical_str(value: _dt.datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("audit datetimes must be timezone-aware")
    return value.astimezone(_dt.timezone.utc).isoformat()


def to_json_safe(value: Any) -> Any:
    """
    Return a structure containing only str/int/bool/None/list/dict, with
    Decimal, datetime, date and UUID represented as tagged one-key dicts.

    Rejects float outright (payments-adjacent code uses Decimal) and any type
    it does not explicitly know — it never falls back to str(). Idempotent:
    feeding it an already-safe structure returns an equal structure, so
    verification can call it on values read back from JSONB.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):  # before int: bool is a subclass of int
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError("audit payloads must not contain float; use Decimal")
    if isinstance(value, Decimal):
        return {_DECIMAL_TAG: _decimal_to_canonical_str(value)}
    if isinstance(value, _dt.datetime):
        return {_DATETIME_TAG: _datetime_to_canonical_str(value)}
    if isinstance(value, _dt.date):
        return {_DATE_TAG: value.isoformat()}
    if isinstance(value, uuid.UUID):
        return {_UUID_TAG: str(value)}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"audit payload keys must be str, got {type(key)!r}")
            out[key] = to_json_safe(item)
        return out
    if isinstance(value, (list, tuple)):
        return [to_json_safe(item) for item in value]
    raise TypeError(f"unsupported type in audit payload: {type(value)!r}")


def canonical_bytes(obj: Any) -> bytes:
    """Deterministic serialisation: sorted keys, no whitespace, explicit UTF-8."""
    return json.dumps(
        to_json_safe(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def compute_event_hash(
    *,
    event_id: uuid.UUID,
    ref_type: str,
    ref_id: uuid.UUID,
    event_type: str,
    created_at: _dt.datetime,
    payload: Mapping[str, Any],
    prev_hash: str,
) -> str:
    """
    The hash contract. SHA-256 over the canonical serialisation of:

        {id, ref_type, ref_id, event_type, created_at, payload, prev_hash}

    `id` is included on purpose: it pins the hash to one row, so a valid
    (payload, prev_hash, hash) triple cannot be replayed onto a different row.
    """
    body = {
        "id": str(event_id),
        "ref_type": ref_type,
        "ref_id": str(ref_id),
        "event_type": event_type,
        "created_at": _datetime_to_canonical_str(created_at),
        "payload": to_json_safe(payload),
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(canonical_bytes(body)).hexdigest()

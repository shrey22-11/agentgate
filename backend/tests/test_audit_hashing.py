"""
Phase 5 — pure audit hashing. No DB, no LLM.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from uuid import uuid4

import pytest

import app.audit.hashing as hashing_module
import app.audit.service as service_module
import app.audit.verify as verify_module
from app.audit.hashing import (
    GENESIS_PREV_HASH,
    canonical_bytes,
    compute_event_hash,
    to_json_safe,
)
from decimal import Decimal

D = Decimal
_UTC = dt.timezone.utc


def _hash(**over) -> str:
    base = dict(
        event_id=uuid4(),
        ref_type="action_request",
        ref_id=uuid4(),
        event_type="POLICY_EVALUATED",
        created_at=dt.datetime(2026, 9, 3, 10, 0, 0, tzinfo=_UTC),
        payload={"verdict": "ALLOW"},
        prev_hash=GENESIS_PREV_HASH,
    )
    base.update(over)
    return compute_event_hash(**base)


def test_genesis_prev_hash_is_64_hex_zeros() -> None:
    assert GENESIS_PREV_HASH == "0" * 64
    assert re.fullmatch(r"[0-9a-f]{64}", GENESIS_PREV_HASH)


def test_event_hash_is_lowercase_sha256_hex() -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", _hash())


def test_hash_is_deterministic_for_identical_inputs() -> None:
    kwargs = dict(
        event_id=uuid4(),
        ref_type="decision",
        ref_id=uuid4(),
        event_type="COUNTER_OFFER_CREATED",
        created_at=dt.datetime(2026, 9, 3, 12, 30, 5, 123456, tzinfo=_UTC),
        payload={"price": D("9000.00"), "nested": {"b": 2, "a": [1, 2, 3]}},
        prev_hash="a" * 64,
    )
    assert len({compute_event_hash(**kwargs) for _ in range(5)}) == 1


@pytest.mark.parametrize(
    "field, value",
    [
        ("ref_type", "other"),
        ("ref_id", uuid4()),
        ("event_type", "ACTION_PARSED"),
        ("created_at", dt.datetime(2026, 9, 3, 10, 0, 1, tzinfo=_UTC)),
        ("payload", {"verdict": "DENY"}),
        ("prev_hash", "b" * 64),
        ("event_id", uuid4()),
    ],
)
def test_hash_changes_when_any_contract_field_changes(field, value) -> None:
    assert _hash() != _hash(**{field: value})


def test_canonical_bytes_is_key_order_independent() -> None:
    a = canonical_bytes({"b": 1, "a": 2, "c": {"y": 9, "x": 8}})
    b = canonical_bytes({"c": {"x": 8, "y": 9}, "a": 2, "b": 1})
    assert a == b


def test_canonical_bytes_is_utf8_and_compact() -> None:
    raw = canonical_bytes({"name": "café", "n": 1})
    assert isinstance(raw, bytes)
    assert raw == b'{"n":1,"name":"caf\xc3\xa9"}'


# --- Decimal representation --------------------------------------------------
def test_decimal_scale_is_normalised_away() -> None:
    # 9000.00 and 9000 are the same value -> same hash.
    assert canonical_bytes({"x": D("9000.00")}) == canonical_bytes({"x": D("9000")})
    assert canonical_bytes({"x": D("9000.50")}) == canonical_bytes({"x": D("9000.5")})


def test_decimal_value_difference_changes_hash() -> None:
    assert canonical_bytes({"x": D("9000")}) != canonical_bytes({"x": D("1000")})
    assert canonical_bytes({"x": D("9000")}) != canonical_bytes({"x": D("9000.5")})


def test_decimal_never_uses_scientific_notation() -> None:
    raw = canonical_bytes({"x": D("90000000000.00"), "y": D("0.00000001")})
    assert b"90000000000" in raw
    assert b"1E" not in raw and b"e+" not in raw and b"E+" not in raw


def test_decimal_zero_forms_collapse() -> None:
    for z in (D("0"), D("-0"), D("0.00"), D("0E-10")):
        assert canonical_bytes({"x": z}) == canonical_bytes({"x": D("0")})


def test_non_finite_decimal_rejected() -> None:
    with pytest.raises(ValueError):
        to_json_safe({"x": D("NaN")})


# --- float / unknown-type rejection --------------------------------------
def test_float_is_rejected() -> None:
    with pytest.raises(TypeError):
        to_json_safe({"amount": 9000.0})


def test_unknown_types_are_rejected_not_stringified() -> None:
    for bad in ({1, 2, 3}, b"bytes", object()):
        with pytest.raises(TypeError):
            to_json_safe({"x": bad})


def test_non_string_keys_rejected() -> None:
    with pytest.raises(TypeError):
        to_json_safe({1: "a"})


# --- datetime / uuid handling ------------------------------------------
def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError):
        to_json_safe({"t": dt.datetime(2026, 9, 3, 10, 0, 0)})


def test_same_instant_different_tz_hash_equal() -> None:
    utc = dt.datetime(2026, 9, 3, 10, 0, 0, tzinfo=_UTC)
    plus_530 = utc.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    assert canonical_bytes({"t": utc}) == canonical_bytes({"t": plus_530})


def test_uuid_and_datetime_are_tagged() -> None:
    u = uuid4()
    safe = to_json_safe({"id": u, "t": dt.datetime(2026, 1, 1, tzinfo=_UTC)})
    assert safe == {"id": {"__uuid__": str(u)}, "t": {"__datetime__": "2026-01-01T00:00:00+00:00"}}


def test_to_json_safe_is_idempotent() -> None:
    once = to_json_safe({"p": D("12.50"), "n": [1, {"x": D("0")}]})
    assert to_json_safe(once) == once


# --- no LLM, no float anywhere in the audit module -------------------
def test_audit_source_has_no_llm_and_no_float() -> None:
    audit_dir = Path(hashing_module.__file__).parent
    for path in sorted(audit_dir.glob("*.py")):
        src = path.read_text(encoding="utf-8").lower()
        assert "anthropic" not in src, path
        assert "openai" not in src, path
    for mod in (hashing_module, service_module, verify_module):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "float(" not in src, mod.__name__

# AgentGate audit system (Phase 5)

**OUR SYSTEM.** A hash-chained, tamper-evident, append-only audit log. Not a
blockchain. Deterministic — no LLM, no floating point.

It answers: *what happened, when, why did the system decide that, and has the
recorded history been modified?*

## What it guarantees

- Every audit event carries a SHA-256 hash over a fixed set of its fields.
- Every event links to its predecessor via `prev_hash`.
- The chain has one unambiguous walk order (`seq`, a `GENERATED ALWAYS`
  identity column that PostgreSQL will not let anyone `UPDATE`).
- The application has no code path that updates or deletes an audit event.
- Ordinary SQL `UPDATE`, `DELETE` and `TRUNCATE` against `audit_event` are
  rejected by database triggers.
- `verify_audit_chain()` recomputes every hash and re-checks every link, and
  reports the first break with a code and the offending event's id/seq.

## What it does NOT guarantee

- **It is not immutable against a privileged database role.** A superuser or the
  table owner can `ALTER TABLE audit_event DISABLE TRIGGER ...`, `DROP` the
  trigger function, or rewrite rows with triggers disabled. The correct claim
  is: *the application and ordinary database operations cannot silently modify
  or delete audit history, and cryptographic verification detects tampering
  with the chain.*
- **Tail truncation is not detectable by the chain alone.** Deleting the most
  recent *k* events (with protections disabled) leaves events `1..n-k` a
  perfectly valid chain. Detecting that needs an external anchor (publishing the
  head hash elsewhere) or a stored expected count — neither is built yet. The
  `DELETE` trigger is what prevents this in practice.
- It does not sign events (no asymmetric key). Anyone who can write rows with
  triggers disabled and knows the (public, non-secret) hash algorithm can
  rebuild a consistent chain from the tamper point forward. What they cannot do
  is change one event and leave the rest untouched — that is always detected.

## Hash contract

`hash = sha256(canonical_bytes(body)).hexdigest()` — 64 lowercase hex chars,
where `body` is exactly:

| key | source | serialised as |
|---|---|---|
| `id` | the event's UUID | canonical string |
| `ref_type` | e.g. `"action_request"` | string |
| `ref_id` | the referenced entity's UUID | canonical string |
| `event_type` | e.g. `"POLICY_EVALUATED"` | string |
| `created_at` | **app-assigned** at append time | UTC ISO-8601 (`_datetime_to_canonical_str`) |
| `payload` | caller's dict | canonicalised (see below) |
| `prev_hash` | predecessor's `hash`, or the genesis sentinel | 64-hex string |

**`id` is in the hash on purpose.** It pins a hash to one specific row, so a
valid `(payload, created_at, prev_hash, hash)` set cannot be lifted onto a new
row. The consequence — two independently appended events never share a hash even
with identical `ref_*`/`event_type`/`payload` — is expected; the determinism
property that matters is that `compute_event_hash(**same_args)` is stable, which
is tested directly.

**`seq` is NOT in the hash.** It is the DB-assigned walk order only; `prev_hash`
is the cryptographic link. `seq` gaps (from a rolled-back `INSERT` consuming a
value) are normal and are not a verification failure.

`created_at` is assigned by the append service (`datetime.now(timezone.utc)`),
not by the database, so it is known before the hash is computed. The
`server_default now()` on the column is a defensive fallback that never fires in
practice.

## Canonical serialization

`json.dumps(to_json_safe(obj), sort_keys=True, separators=(",", ":"),
ensure_ascii=False, allow_nan=False).encode("utf-8")`

- **sorted keys** — insertion order is irrelevant.
- **compact separators** — no whitespace.
- **`ensure_ascii=False` + explicit UTF-8 encode** — one byte representation for
  non-ASCII text.
- **`allow_nan=False`** — `NaN`/`Infinity` are not valid audit data.

This is "sorted-key compact JSON", not full RFC 8785 JCS. That is sufficient
here because payloads contain only `str`, `int`, `bool`, `None`, lists, dicts,
and the tagged scalars below — there are no bare floats to canonicalise and no
number-format ambiguity (Decimals are emitted as normalised strings).

`to_json_safe` rejects anything it does not explicitly understand (it never
falls back to `str()`), and rejects `float` outright.

### Non-JSON scalars in payloads

Represented as single-key tagged objects so they round-trip through JSONB and
are unambiguous:

| Python type | representation |
|---|---|
| `Decimal` | `{"__decimal__": "<normalised>"}` |
| `datetime` (tz-aware; naive rejected) | `{"__datetime__": "<UTC ISO-8601>"}` |
| `date` | `{"__date__": "<ISO-8601>"}` |
| `UUID` | `{"__uuid__": "<str>"}` |

Payload keys of the form `__x__` are reserved.

## Decimal handling

`Decimal` is **normalised** before serialisation
(`_decimal_to_canonical_str`): trailing-zero scale is removed and scientific
notation is never emitted.

- `Decimal("9000.00")` and `Decimal("9000")` → `"9000"` (equal hash)
- `Decimal("9000.50")` and `Decimal("9000.5")` → `"9000.5"` (equal hash)
- `Decimal("9000")` vs `Decimal("9000.5")` → different hash
- `Decimal("0")`, `Decimal("-0")`, `Decimal("0.00")` → `"0"`
- non-finite (`NaN`, `Infinity`) → `ValueError`

Rationale: the audit reader cares about the *value*, not the stored scale.
Normalising removes a class of false tamper-positives that would otherwise fire
if a value were re-quantised somewhere upstream. The trade-off (scale is not
preserved) is accepted deliberately.

## Chain ordering

`verify_audit_chain()` walks `SELECT ... FROM audit_event ORDER BY seq ASC`.
`seq` is `BIGINT GENERATED ALWAYS AS IDENTITY` — PostgreSQL rejects any `UPDATE`
of it, so the walk order itself cannot be tampered with through normal SQL. A
reorder attempt (delete + re-insert with swapped `seq`, triggers disabled) is
still caught, because each event's `prev_hash` no longer links to the event now
preceding it.

## Concurrency strategy

Appends can genuinely interleave (the app engine has a connection pool, and
async request handling). A naïve "read head, insert successor" pattern lets two
transactions read the same head and both set `prev_hash` to it — a fork that
`verify` would later flag as `PREV_HASH_MISMATCH`.

`append_audit_event()` prevents the fork by taking a **transaction-scoped
PostgreSQL advisory lock** before reading the head:

```
SELECT pg_advisory_xact_lock(-3706863870427187819)
```

The key is derived deterministically from `blake2b(b"agentgate.audit.chain")`.
Only one transaction at a time can be inside the read-head→insert section. The
lock is released automatically on the caller's `COMMIT` or `ROLLBACK`. It also
covers the genesis race (empty table — nothing to `SELECT ... FOR UPDATE`).

The append runs **inside the caller's transaction** and does not commit, so an
audit event is persisted atomically with the business change it records. The
cost — audit appends are serialised for the life of the caller's transaction —
is acceptable for this app (short, synchronous request/response, no long
transactions) and is preferable to breaking that atomicity.

No Redis, no queue, no lock table: one PostgreSQL primitive.

Tests: `test_advisory_lock_serialises_concurrent_appends` opens two real
connections and asserts the second append blocks until the first transaction
commits, then that the resulting chain has not forked.
`test_forked_chain_fails_verification` proves the lock is load-bearing by
constructing the fork it prevents and showing `verify` catches it.

## Database protection

Migration `38d7194a76b6` (`audit append-only protection`) creates:

```sql
CREATE FUNCTION agentgate_audit_event_immutable() RETURNS trigger ...
  BEGIN RAISE EXCEPTION 'audit_event is append-only; % is not permitted', TG_OP; ... END;

CREATE TRIGGER audit_event_no_update   BEFORE UPDATE   ON audit_event FOR EACH ROW       ...
CREATE TRIGGER audit_event_no_delete   BEFORE DELETE   ON audit_event FOR EACH ROW       ...
CREATE TRIGGER audit_event_no_truncate BEFORE TRUNCATE ON audit_event FOR EACH STATEMENT ...
```

`INSERT` is unaffected. The DDL lives in `app/audit/ddl.py` so the Alembic
migration and the test-suite schema builder apply the identical statements. The
migration also drops the meaningless `updated_at` column; `downgrade` removes
the triggers/function and restores the column.

## Verification — what is detected

`verify_audit_chain(session) -> AuditVerificationResult(valid, checked_events,
failure, failure_detail, event_id, event_seq)`.

| Corruption | Result |
|---|---|
| Payload modified | `HASH_MISMATCH` at that event |
| Stored `hash` modified | `HASH_MISMATCH` at that event |
| `prev_hash` modified (only) | `HASH_MISMATCH` (prev_hash is inside the hash body) |
| `prev_hash` modified **and** `hash` recomputed consistently | `PREV_HASH_MISMATCH` (link to real predecessor broken) |
| First event's `prev_hash` not the genesis sentinel | `BROKEN_GENESIS` |
| A middle event deleted | `PREV_HASH_MISMATCH` at the following event |
| Two events reordered | `PREV_HASH_MISMATCH` |
| Forked chain (two events, same predecessor) | `PREV_HASH_MISMATCH` at the second |
| Valid chain / empty chain | `valid=True` |

Not detected by the chain alone: deletion of the most recent event(s) — see
*What it does NOT guarantee*.

## Genesis sentinel

`GENESIS_PREV_HASH = "0" * 64`. The first event's `prev_hash`. Obviously a
sentinel, still a valid `String(64)` value.

## Files

```
app/audit/models.py    AuditEvent ORM (no TimestampMixin; app-assigned created_at)
app/audit/ddl.py        trigger/function DDL — single source of truth
app/audit/events.py     event-type string constants (not a closed enum)
app/audit/hashing.py    to_json_safe, canonical_bytes, compute_event_hash, GENESIS_PREV_HASH
app/audit/verify.py     verify_audit_chain, AuditVerificationResult
app/audit/service.py    append_audit_event  (the only writer; advisory lock here)
app/audit/__init__.py   public surface
migrations/versions/38d7194a76b6_audit_append_only_protection.py
tests/test_audit_hashing.py   25 pure tests
tests/test_audit_chain.py     22 DB tests (tamper, enforcement, concurrency)
```

## Event types (later phases wire the call sites)

`ACTION_REQUEST_RECEIVED`, `ACTION_PARSED`, `POLICY_EVALUATED`,
`COUNTER_OFFER_CREATED`, `APPROVAL_REQUESTED`, `APPROVAL_RESOLVED`,
`PAYMENT_EXECUTION_STARTED`, `PAYMENT_EXECUTION_SUCCEEDED`,
`PAYMENT_EXECUTION_FAILED`, `WEBHOOK_RECEIVED`, `WEBHOOK_DUPLICATE_IGNORED`,
`AI_PROVIDER_FAILED`.

Plain string constants in `app/audit/events.py`, not a DB enum: every phase adds
a few, and a closed enum would mean a migration each time and would couple the
audit module to every feature module. The append service validates shape
(`^[A-Z][A-Z0-9_]{2,59}$`) — "validated strings", not "anything goes".

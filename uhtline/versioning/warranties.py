"""Confirmations, snapshots and baselines that expire with their generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from ..core.clock import Clock, parse_stamp
from ..core.config import EvidenceEnvelope
from ..errors import NotFoundError, StaleWarrantyError, ValidationError
from ..persistence.store import DurableStore
from .generations import GenerationRegistry

VALID = "valid"
ELAPSED = "elapsed"
SUPERSEDED = "superseded"
CONSUMED = "consumed"


def _expires_at(issued_at: str, ttl_seconds: float) -> str:
    return (parse_stamp(issued_at) + timedelta(seconds=float(ttl_seconds))).isoformat(  # type: ignore[operator]
        timespec="milliseconds"
    )


@dataclass(frozen=True)
class Confirmation:
    confirmation_id: str
    scope: str
    generation: int
    subject: str
    issued_at: str
    expires_at: str
    state: str
    consumed_at: str | None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Confirmation":
        return cls(
            confirmation_id=str(value["confirmation_id"]),
            scope=str(value["scope"]),
            generation=int(value["generation"]),
            subject=str(value["subject"]),
            issued_at=str(value["issued_at"]),
            expires_at=str(value["expires_at"]),
            state=str(value["state"]),
            consumed_at=None if value.get("consumed_at") is None else str(value["consumed_at"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "confirmation_id": self.confirmation_id,
            "scope": self.scope,
            "generation": self.generation,
            "subject": self.subject,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "state": self.state,
            "consumed_at": self.consumed_at,
        }


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    name: str
    scope: str
    generation: int
    payload: dict[str, Any]
    captured_at: str
    expires_at: str
    state: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Snapshot":
        return cls(
            snapshot_id=str(value["snapshot_id"]),
            name=str(value["name"]),
            scope=str(value["scope"]),
            generation=int(value["generation"]),
            payload=dict(value.get("payload") or {}),
            captured_at=str(value["captured_at"]),
            expires_at=str(value["expires_at"]),
            state=str(value["state"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "name": self.name,
            "scope": self.scope,
            "generation": self.generation,
            "payload": self.payload,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
            "state": self.state,
        }


@dataclass(frozen=True)
class Baseline:
    baseline_id: str
    scope: str
    generation: int
    payload: dict[str, Any]
    recorded_at: str
    expires_at: str
    state: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Baseline":
        return cls(
            baseline_id=str(value["baseline_id"]),
            scope=str(value["scope"]),
            generation=int(value["generation"]),
            payload=dict(value.get("payload") or {}),
            recorded_at=str(value["recorded_at"]),
            expires_at=str(value["expires_at"]),
            state=str(value["state"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline_id": self.baseline_id,
            "scope": self.scope,
            "generation": self.generation,
            "payload": self.payload,
            "recorded_at": self.recorded_at,
            "expires_at": self.expires_at,
            "state": self.state,
        }


class WarrantyBook:
    """Issues evidence objects and refuses every one that has gone stale."""

    document = "warranties"

    def __init__(
        self,
        store: DurableStore,
        clock: Clock,
        generations: GenerationRegistry,
        evidence: EvidenceEnvelope,
    ) -> None:
        self.store = store
        self.clock = clock
        self.generations = generations
        self.evidence = evidence
        self._confirmations: dict[str, dict[str, Any]] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._baselines: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        payload = stored.payload
        self._confirmations = {str(k): dict(v) for k, v in (payload.get("confirmations") or {}).items()}
        self._snapshots = {str(k): dict(v) for k, v in (payload.get("snapshots") or {}).items()}
        self._baselines = {str(k): dict(v) for k, v in (payload.get("baselines") or {}).items()}

    def persist(self) -> None:
        self.store.write(
            self.document,
            {
                "confirmations": self._confirmations,
                "snapshots": self._snapshots,
                "baselines": self._baselines,
            },
        )

    # -- life cycle helpers ------------------------------------------------

    def _ttl(self, explicit: float | None, configured: float) -> float:
        ttl = float(configured if explicit is None else explicit)
        if ttl <= 0:
            raise ValidationError("evidence ttl must be positive", ttl_seconds=ttl)
        return ttl

    def _current_generation(self, scope: str) -> int:
        label = str(scope)
        if label in self.generations.scopes():
            return self.generations.generation(label)
        return 0

    def _state(self, record: Mapping[str, Any]) -> str:
        if record.get("consumed_at"):
            return CONSUMED
        expires_at = parse_stamp(record.get("expires_at"))
        if expires_at is not None and expires_at <= self.clock.now():
            return ELAPSED
        scope = str(record.get("scope", ""))
        if scope in self.generations.scopes() and int(record.get("generation", 0)) != self.generations.generation(scope):
            return SUPERSEDED
        return VALID

    def _refresh(self, bucket: dict[str, dict[str, Any]]) -> None:
        for record in bucket.values():
            record["state"] = self._state(record)

    def _require(self, bucket: dict[str, dict[str, Any]], key: str, *, kind: str) -> dict[str, Any]:
        record = bucket.get(str(key))
        if record is None:
            raise NotFoundError(f"{kind} was never issued", kind=kind, reference=str(key))
        state = self._state(record)
        record["state"] = state
        if state != VALID:
            scope = str(record["scope"])
            raise StaleWarrantyError(
                f"{kind} is no longer valid",
                kind=kind,
                reference=str(key),
                state=state,
                scope=scope,
                generation=int(record["generation"]),
                current_generation=self._current_generation(scope),
                expires_at=record["expires_at"],
            )
        return record

    def expire_stale(self) -> int:
        """Report how many evidence objects aged out since the last sweep."""

        invalidated = 0
        for bucket in (self._confirmations, self._snapshots, self._baselines):
            for record in bucket.values():
                if str(record.get("state", VALID)) != VALID:
                    continue
                state = self._state(record)
                record["state"] = state
                if state != VALID:
                    invalidated += 1
        if invalidated:
            self.persist()
        return invalidated

    # -- confirmations -----------------------------------------------------

    def issue_confirmation(
        self,
        scope: str,
        subject: str,
        *,
        ttl_seconds: float | None = None,
        reason: str = "operator confirmation",
    ) -> Confirmation:
        ttl = self._ttl(ttl_seconds, self.evidence.confirmation_ttl_seconds)
        issued_at = self.clock.timestamp()
        confirmation_id = f"cfm-{scope}-{len(self._confirmations) + 1:05d}"
        record = {
            "confirmation_id": confirmation_id,
            "scope": scope,
            "generation": self._current_generation(scope),
            "subject": str(subject),
            "issued_at": issued_at,
            "expires_at": _expires_at(issued_at, ttl),
            "state": VALID,
            "consumed_at": None,
            "reason": str(reason),
            "ttl_seconds": ttl,
        }
        self._confirmations[confirmation_id] = record
        self.persist()
        return Confirmation.from_dict(record)

    def require_confirmation(self, confirmation_id: str, *, scope: str, subject: str | None = None) -> Confirmation:
        record = self._require(self._confirmations, confirmation_id, kind="confirmation")
        if str(record["scope"]) != scope:
            raise StaleWarrantyError(
                "confirmation belongs to another scope",
                kind="confirmation",
                reference=confirmation_id,
                state="scope-mismatch",
                scope=scope,
                actual_scope=record["scope"],
            )
        if subject is not None and str(record["subject"]) != str(subject):
            raise StaleWarrantyError(
                "confirmation was issued for another subject",
                kind="confirmation",
                reference=confirmation_id,
                state="subject-mismatch",
                subject=str(subject),
                actual_subject=record["subject"],
            )
        return Confirmation.from_dict(record)

    def consume_confirmation(self, confirmation_id: str, *, scope: str, subject: str | None = None) -> Confirmation:
        confirmation = self.require_confirmation(confirmation_id, scope=scope, subject=subject)
        record = self._confirmations[confirmation.confirmation_id]
        record["consumed_at"] = self.clock.timestamp()
        record["state"] = CONSUMED
        self.persist()
        return Confirmation.from_dict(record)

    # -- snapshots and baselines ------------------------------------------

    def capture_snapshot(
        self,
        name: str,
        scope: str,
        payload: Mapping[str, Any],
        *,
        ttl_seconds: float | None = None,
        reason: str = "operator snapshot",
    ) -> Snapshot:
        ttl = self._ttl(ttl_seconds, self.evidence.snapshot_ttl_seconds)
        captured_at = self.clock.timestamp()
        snapshot_id = f"snp-{scope}-{len(self._snapshots) + 1:05d}"
        record = {
            "snapshot_id": snapshot_id,
            "name": str(name),
            "scope": scope,
            "generation": self._current_generation(scope),
            "payload": {str(key): value for key, value in payload.items()},
            "captured_at": captured_at,
            "expires_at": _expires_at(captured_at, ttl),
            "state": VALID,
            "reason": str(reason),
        }
        self._snapshots[snapshot_id] = record
        self.persist()
        return Snapshot.from_dict(record)

    def require_snapshot(self, snapshot_id: str, *, scope: str | None = None) -> Snapshot:
        record = self._require(self._snapshots, snapshot_id, kind="snapshot")
        if scope is not None and str(record["scope"]) != scope:
            raise StaleWarrantyError(
                "snapshot belongs to another scope",
                kind="snapshot",
                reference=snapshot_id,
                state="scope-mismatch",
                scope=scope,
                actual_scope=record["scope"],
            )
        return Snapshot.from_dict(record)

    def record_baseline(
        self,
        scope: str,
        payload: Mapping[str, Any],
        *,
        ttl_seconds: float | None = None,
        reason: str = "operator baseline",
    ) -> Baseline:
        ttl = self._ttl(ttl_seconds, self.evidence.baseline_ttl_seconds)
        recorded_at = self.clock.timestamp()
        baseline_id = f"bsl-{scope}-{len(self._baselines) + 1:05d}"
        record = {
            "baseline_id": baseline_id,
            "scope": scope,
            "generation": self._current_generation(scope),
            "payload": {str(key): value for key, value in payload.items()},
            "recorded_at": recorded_at,
            "expires_at": _expires_at(recorded_at, ttl),
            "state": VALID,
            "reason": str(reason),
        }
        self._baselines[baseline_id] = record
        self.persist()
        return Baseline.from_dict(record)

    def require_baseline(self, baseline_id: str, *, scope: str | None = None) -> Baseline:
        record = self._require(self._baselines, baseline_id, kind="baseline")
        if scope is not None and str(record["scope"]) != scope:
            raise StaleWarrantyError(
                "baseline belongs to another scope",
                kind="baseline",
                reference=baseline_id,
                state="scope-mismatch",
                scope=scope,
                actual_scope=record["scope"],
            )
        return Baseline.from_dict(record)

    # -- reporting ---------------------------------------------------------

    def inventory(self) -> dict[str, Any]:
        self._refresh(self._confirmations)
        self._refresh(self._snapshots)
        self._refresh(self._baselines)
        return {
            "confirmations": {key: dict(value) for key, value in sorted(self._confirmations.items())},
            "snapshots": {key: dict(value) for key, value in sorted(self._snapshots.items())},
            "baselines": {key: dict(value) for key, value in sorted(self._baselines.items())},
        }

    def state_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for bucket in (self._confirmations, self._snapshots, self._baselines):
            self._refresh(bucket)
            for record in bucket.values():
                key = str(record.get("state", VALID))
                counts[key] = counts.get(key, 0) + 1
        return counts


__all__ = ["CONSUMED", "ELAPSED", "SUPERSEDED", "VALID", "Baseline", "Confirmation", "Snapshot", "WarrantyBook"]

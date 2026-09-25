"""
Stele replay engine (roadmap #14).

Replay re-runs a record's parser, at its recorded identity and with its
recorded config, on its recorded input Snapshot, and compares the new output
with the sealed one. It is a different operation from validation
(stele.replay.planner), which only re-hashes files that already exist.

Every replay has exactly one of five outcomes, never merged into each other:

  REPRODUCED    byte-identical output from a deterministic replay. Only
                parsers whose spec requires a DETERMINISTIC backend (Wasm, #7)
                can produce it.
  EQUIVALENT    accepted under the parser's comparison policy (structural or
                tolerance rules for ML/GPU parsers). Not proof of
                reproduction; never reported as REPRODUCED, even when the
                bytes happen to match.
  DIVERGED      outside policy (or, without a policy, any byte difference).
                Feeds invalidation: invalidate_diverged().
  UNREPLAYABLE  the parser cannot be run as recorded: no parser identity or
                spec, a missing or different Wasm module or image, a missing
                input Snapshot, no capable backend, or a non-deterministic
                parser without a comparison policy.
  FAILED        the parser was run as recorded but the run did not complete
                (non-zero exit, timeout, resource limit, crash), so there is
                no output to compare. Says nothing about the sealed evidence
                and never feeds invalidation; replay again.

Each replay is written to the ledger's append-only replay log. The replay's
own output is stored in the evidence archive (replay_artifact_hash), so a
divergence can be inspected later.
"""
from __future__ import annotations

import json
import platform
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, Iterable
from uuid import uuid4

from ..archive import ArchiveError, materialize_snapshot
from ..archive.records import canonical_json
from ..containment.backend import UnsupportedBackendError
from ..containment.wasm import wasm_module_sha256
from ..ledger.models import ArtifactRecord, ArtifactState
from ..ledger.store import LedgerStore, connect
from .models import InvalidationReason
from .parsers import ParserCatalog, ParserSpec, run_parser


class ReplayOutcome(str, Enum):
    REPRODUCED = "reproduced"
    EQUIVALENT = "equivalent"
    DIVERGED = "diverged"
    UNREPLAYABLE = "unreplayable"
    FAILED = "failed"


@dataclass(frozen=True)
class ReplayResult:
    replay_id: str
    record_id: str
    outcome: ReplayOutcome
    reason: str
    replay_run_id: str | None             # None when nothing was run
    replay_artifact_hash: str | None      # the replay's output tree in the archive
    differences: tuple[str, ...]          # differing paths, or the policy's findings
    policy: dict[str, Any] | None         # the policy the replay was judged by
    backend: str | None
    platform: str
    replayed_at: datetime


def current_platform() -> str:
    return f"{sys.platform}-{platform.machine().lower()}"


# ---------------------------------------------------------------------------
# Replay log
# ---------------------------------------------------------------------------

class ReplayLog:
    """The append-only replay log in a ledger's database."""

    def __init__(self, ledger: LedgerStore) -> None:
        self._conn = connect(ledger.db_path)

    def close(self) -> None:
        self._conn.close()

    def append(self, result: ReplayResult) -> None:
        self._conn.execute(
            "INSERT INTO replays (replay_id, record_id, outcome, reason, replay_run_id, "
            "replay_artifact_hash, differences, policy, backend, platform, replayed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                result.replay_id, result.record_id, result.outcome.value, result.reason,
                result.replay_run_id, result.replay_artifact_hash,
                json.dumps(list(result.differences)),
                canonical_json(result.policy).decode() if result.policy is not None else None,
                result.backend, result.platform, result.replayed_at.isoformat(),
            ),
        )

    def for_record(self, record_id: str) -> list[ReplayResult]:
        return self._select("record_id=?", (record_id,))

    def with_outcome(self, outcome: ReplayOutcome) -> list[ReplayResult]:
        """E.g. every DIVERGED replay: the diverged-runs view."""
        return self._select("outcome=?", (ReplayOutcome(outcome).value,))

    def all(self) -> list[ReplayResult]:
        return self._select("1=1", ())

    def _select(self, where: str, params: tuple) -> list[ReplayResult]:
        rows = self._conn.execute(
            f"SELECT * FROM replays WHERE {where} ORDER BY replayed_at, replay_id", params
        ).fetchall()
        return [_row_to_result(r) for r in rows]


def _row_to_result(row: sqlite3.Row) -> ReplayResult:
    return ReplayResult(
        replay_id=row["replay_id"],
        record_id=row["record_id"],
        outcome=ReplayOutcome(row["outcome"]),
        reason=row["reason"],
        replay_run_id=row["replay_run_id"],
        replay_artifact_hash=row["replay_artifact_hash"],
        differences=tuple(json.loads(row["differences"])),
        policy=json.loads(row["policy"]) if row["policy"] is not None else None,
        backend=row["backend"],
        platform=row["platform"],
        replayed_at=datetime.fromisoformat(row["replayed_at"]),
    )


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def replay_record(
    ledger: LedgerStore, catalog: ParserCatalog, record: ArtifactRecord | str
) -> ReplayResult:
    """Replay one sealed (or invalidated, for audit) record and log the result."""
    record_id = record.record_id if isinstance(record, ArtifactRecord) else record
    live = ledger.get(record_id)
    if live.state not in (ArtifactState.SEALED, ArtifactState.INVALIDATED):
        raise ValueError(
            f"record {record_id} is {live.state.value}; only sealed or invalidated "
            "records have evidence to replay"
        )
    result = _Replay(ledger, catalog, live).run()
    log = ReplayLog(ledger)
    try:
        log.append(result)
    finally:
        log.close()
    return result


def replay_records(
    ledger: LedgerStore, catalog: ParserCatalog, records: Iterable[ArtifactRecord | str]
) -> list[ReplayResult]:
    return [replay_record(ledger, catalog, r) for r in records]


def invalidate_diverged(dispatcher: Any, results: Iterable[ReplayResult]) -> list[Any]:
    """Invalidate every DIVERGED record through the Dispatcher, which also
    removes what it delivered (#13). Returns the removal results.

    Only DIVERGED is a completed comparison that found a difference; FAILED
    and UNREPLAYABLE replays compared nothing and never invalidate.
    """
    removals: list[Any] = []
    for result in results:
        if result.outcome is ReplayOutcome.DIVERGED:
            removals.extend(dispatcher.invalidate(
                result.record_id,
                f"{InvalidationReason.REPLAY_DIVERGED.value} — replay {result.replay_id}: "
                f"{result.reason}",
            ))
    return removals


class _Unreplayable(Exception):
    pass


class _Replay:
    def __init__(self, ledger: LedgerStore, catalog: ParserCatalog, record: ArtifactRecord) -> None:
        self.ledger = ledger
        self.catalog = catalog
        self.record = record
        self.replay_id = str(uuid4())

    def run(self) -> ReplayResult:
        try:
            spec = self._spec()
            with TemporaryDirectory(prefix="stele-replay-") as work:
                return self._execute(spec, Path(work))
        except _Unreplayable as exc:
            return self._result(ReplayOutcome.UNREPLAYABLE, str(exc))

    def _spec(self) -> ParserSpec:
        parser = self.record.parser
        if parser is None:
            raise _Unreplayable("the record has no parser identity (migrated from a pre-#12 ledger)")
        spec = self.catalog.get(parser.name, parser.version)
        if spec is None:
            raise _Unreplayable(f"parser {parser.name} {parser.version} is not in the catalog")
        if spec.with_conditions is not None:
            # Run the parser on the device and under the limits it was
            # recorded with (e.g. the GPU image for a GPU run).
            try:
                spec = spec.with_conditions(self.record.run_conditions)
            except ValueError as exc:
                raise _Unreplayable(f"cannot run the parser as recorded: {exc}") from exc
        if not spec.deterministic and spec.policy is None:
            raise _Unreplayable(
                f"parser {parser.name} {parser.version} is not deterministic and has no "
                "comparison policy, so no replay of it can be judged"
            )
        if parser.module_sha256 is not None:
            try:
                available = (
                    wasm_module_sha256(spec.module_path) if spec.module_path is not None else None
                )
            except (OSError, ValueError, ImportError) as exc:  # missing, unreadable, no wasmtime
                raise _Unreplayable(f"Wasm module unavailable: {exc}") from exc
            if available != parser.module_sha256:
                raise _Unreplayable(
                    f"Wasm module {parser.module_sha256[:12]}… is unavailable (the catalog's "
                    f"module is {available[:12] + '…' if available else 'not set'})"
                )
        return spec

    def _input(self, work: Path) -> Path | None:
        record = self.record
        if record.source_hash is None:
            return None
        archive = self.ledger.archive
        try:
            snapshot = archive.get_snapshot(record.source_hash, record.source_kind)
            (work / "input").mkdir()
            destination = work / "input" / _input_name(record.source_path)
            materialize_snapshot(archive, snapshot, destination)
        except (ArchiveError, OSError, ValueError) as exc:
            raise _Unreplayable(
                f"input Snapshot {record.source_hash[:12]}… is unavailable: {exc}"
            ) from exc
        return destination

    def _execute(self, spec: ParserSpec, work: Path) -> ReplayResult:
        record = self.record
        input_path = self._input(work)
        try:
            # Building the config is pure; doing it first turns a record the
            # spec cannot run as recorded (e.g. an input name the parser does
            # not accept) into UNREPLAYABLE instead of a failed run.
            try:
                spec.build_config(input_path, work / "output", record.parser_config or {})
            except (ValueError, TypeError, KeyError) as exc:
                raise _Unreplayable(f"cannot run the parser as recorded: {exc}") from exc
            run = run_parser(
                spec,
                artifact_dir=work / "output",
                parser_config=record.parser_config or {},
                input_path=input_path,
                store=self.ledger.archive,
                identity=record.parser,
            )
        except UnsupportedBackendError as exc:
            raise _Unreplayable(f"no capable backend: {exc}") from exc

        self.run_id = str(run.run_id)
        self.backend = run.backend
        self.replay_hash = run.artifact_bundle_digest
        for field_name in ("image_digest", "module_sha256"):
            recorded = getattr(record.parser, field_name)
            measured = getattr(run, field_name)
            if recorded is not None and measured != recorded:
                raise _Unreplayable(
                    f"parser identity unavailable: recorded {field_name} {recorded}, "
                    f"the replay ran {measured}"
                )
        if run.input_sha256 != record.source_hash:
            raise _Unreplayable("the materialized input does not hash to the recorded Snapshot")

        if not run.succeeded:
            # No complete output was produced, so nothing was compared: a
            # crash, timeout or resource limit is not evidence of divergence.
            return self._result(
                ReplayOutcome.FAILED,
                f"the replay run failed (exit code {run.exit_code}, timed out: "
                f"{run.timed_out}); no output was compared",
            )

        recorded, replayed = record.artifact_manifest, run.artifact_digests
        differing = _differing_paths(recorded, replayed)
        if not differing and spec.deterministic:
            return self._result(ReplayOutcome.REPRODUCED, "byte-identical output from a deterministic replay")
        if spec.policy is not None:
            verdict = spec.policy.compare(recorded, replayed, self.ledger.archive)
            if verdict.accepted:
                return self._result(
                    ReplayOutcome.EQUIVALENT,
                    "accepted by the comparison policy; not proof of reproduction",
                    differing, policy=spec.policy.describe(),
                )
            return self._result(
                ReplayOutcome.DIVERGED, "outside the comparison policy",
                verdict.findings, policy=spec.policy.describe(),
            )
        return self._result(
            ReplayOutcome.DIVERGED,
            f"{len(differing)} artifact(s) differ from a deterministic parser's record",
            differing,
        )

    def _result(
        self,
        outcome: ReplayOutcome,
        reason: str,
        differences: tuple[str, ...] = (),
        *,
        policy: dict[str, Any] | None = None,
    ) -> ReplayResult:
        return ReplayResult(
            replay_id=self.replay_id,
            record_id=self.record.record_id,
            outcome=outcome,
            reason=reason,
            replay_run_id=getattr(self, "run_id", None),
            replay_artifact_hash=getattr(self, "replay_hash", None),
            differences=tuple(differences),
            policy=policy,
            backend=getattr(self, "backend", None),
            platform=current_platform(),
            replayed_at=datetime.now(timezone.utc),
        )


def _differing_paths(recorded: dict[str, str], replayed: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        p for p in sorted(set(recorded) | set(replayed)) if recorded.get(p) != replayed.get(p)
    )


_UNPORTABLE = set('<>:"/\\|?*')


def _input_name(source_path: str | None) -> str:
    """The input's original file name when it is portable, else "input".

    Parsers may depend on the name (the sandbox preserves it), but the replay
    host's filesystem must be able to hold it.
    """
    name = PurePosixPath(source_path).name if source_path else ""
    if not name or name in (".", "..") or _UNPORTABLE & set(name) or name != name.strip(". "):
        return "input"
    return name

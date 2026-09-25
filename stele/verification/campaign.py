"""
A verification campaign: every corpus document through every lane.

A lane is one way of running the parser under test: a sandbox backend plus
the ParserSpec that runs the parser there (a script parser needs the host
interpreter under bubblewrap but the image's python3 in a container, so each
lane may bring its own spec, but every lane must be the same parser name and
version). One lane is the baseline, the setup production runs today; the
others are candidates that must agree with it before they replace it.

For each document and lane the campaign:

- runs the parser through the normal path (run_parser -> run_in_sandbox with
  the ledger's archive), forcing the lane's backend;
- checks the parser read exactly the corpus bytes (the input Snapshot digest
  must equal the manifest's SHA-256);
- records a successful run in the ledger and seals it, and drops the working
  output (the archive holds it);
- appends one observation to the journal: status, record id, the run's
  structured failure and its telemetry.

The journal (JSON lines, fsynced per line) makes a full-corpus campaign
resumable: its header pins the corpus id, parser and lanes, and a rerun skips
every (document, lane) already observed. Failed runs are not ledger records,
so the journal is where their failure reasons live; the report (report.py)
re-checks everything a journal line claims about sealed records against the
ledger itself.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from ..archive.records import Source, canonical_json
from ..containment.backend import (
    ContainmentCleanupError,
    SandboxBackend,
    UnsupportedBackendError,
)
from ..ledger.store import LedgerStore
from ..ledger.transaction import record_run
from ..replay.parsers import ParserSpec, run_parser
from .corpus import Corpus

JOURNAL_SCHEMA = "stele.verification-journal"
JOURNAL_VERSION = 1

# Observation statuses.
SEALED = "sealed"                        # ran, recorded and sealed
FAILED = "failed"                        # ran and failed (structured failure reason)
NOT_APPLICABLE = "not_applicable"        # the parser does not accept this document
CONTAINMENT_ERROR = "containment_error"  # the sandbox could not be proven gone
ERROR = "error"                          # anything else: a Stele or host fault
STATUSES = (SEALED, FAILED, NOT_APPLICABLE, CONTAINMENT_ERROR, ERROR)


class CampaignError(RuntimeError):
    """The campaign cannot start or continue (mismatched journal, unusable lane)."""


@dataclass(frozen=True)
class Lane:
    name: str
    backend: SandboxBackend
    spec: ParserSpec

    def forced_spec(self) -> ParserSpec:
        """The lane's spec with its backend forced (never auto-selected)."""
        backend = self.backend
        return dataclasses.replace(self.spec, backend=lambda _identity: backend)


@dataclass(frozen=True)
class Observation:
    path: str
    lane: str
    status: str
    backend: str | None = None
    run_id: str | None = None
    record_id: str | None = None
    artifact_hash: str | None = None
    input_sha256: str | None = None
    failure: dict[str, Any] | None = None
    telemetry: dict[str, Any] | None = None
    detail: str | None = None
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, obj: Mapping[str, Any]) -> "Observation":
        names = {f.name for f in dataclasses.fields(cls)}
        if not isinstance(obj, Mapping) or set(obj) != names or obj.get("status") not in STATUSES:
            raise CampaignError(f"malformed journal observation: {obj!r}")
        return cls(**obj)


def config_digest(parser_config: Mapping[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(dict(parser_config))).hexdigest()


class Journal:
    """Append-only JSON-lines journal of one campaign."""

    def __init__(self, path: Path, header: dict[str, Any]) -> None:
        self.path = Path(path)
        self.header = header
        self.observations: list[Observation] = []
        if self.path.exists() and self.path.stat().st_size > 0:
            data = self.path.read_bytes()
            if not data.endswith(b"\n"):
                # A torn final line from a crash mid-append: that observation
                # was never complete, so drop it and observe again.
                data = data[:data.rfind(b"\n") + 1]
                self._truncate_to(data)
            lines = data.split(b"\n")[:-1]
            try:
                existing = json.loads(lines[0]) if lines else None
            except ValueError as exc:
                raise CampaignError(f"{self.path}: unreadable journal header: {exc}") from exc
            if existing is None:
                self._append_line(header)
            elif existing != header:
                raise CampaignError(
                    f"{self.path} belongs to another campaign (corpus, parser, config or lanes "
                    "differ); use a new journal"
                )
            for number, line in enumerate(lines[1:], start=2):
                try:
                    self.observations.append(Observation.from_json(json.loads(line)))
                except ValueError as exc:
                    raise CampaignError(f"{self.path} line {number} is malformed: {exc}") from exc
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._append_line(header)

    @classmethod
    def read(cls, path: Path) -> "Journal":
        """Open an existing journal without knowing its header in advance."""
        with open(path, "rb") as f:
            header = json.loads(f.readline())
        if header.get("schema") != JOURNAL_SCHEMA or header.get("schema_version") != JOURNAL_VERSION:
            raise CampaignError(f"{path} is not a {JOURNAL_SCHEMA} v{JOURNAL_VERSION}")
        return cls(path, header)

    def done(self) -> set[tuple[str, str]]:
        return {(o.path, o.lane) for o in self.observations}

    def append(self, observation: Observation) -> None:
        self._append_line(observation.to_json())
        self.observations.append(observation)

    def _append_line(self, obj: Mapping[str, Any]) -> None:
        data = canonical_json(dict(obj)) + b"\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _truncate_to(self, data: bytes) -> None:
        with open(self.path, "r+b") as f:
            f.truncate(len(data))
            f.flush()
            os.fsync(f.fileno())


class Campaign:
    """Runs a corpus through a baseline lane and candidate lanes."""

    def __init__(
        self,
        ledger: LedgerStore,
        corpus: Corpus,
        root: Path,
        lanes: Sequence[Lane],
        *,
        baseline: str,
        journal: Path,
        parser_config: Mapping[str, Any] | None = None,
        work_dir: Path | None = None,
        keep_outputs: bool = False,
    ) -> None:
        names = [lane.name for lane in lanes]
        if len(set(names)) != len(names) or not names:
            raise CampaignError("lanes must have unique names")
        if baseline not in names:
            raise CampaignError(f"baseline lane {baseline!r} is not one of {names}")
        identities = {(lane.spec.name, lane.spec.version) for lane in lanes}
        if len(identities) != 1:
            raise CampaignError(f"every lane must run the same parser; got {sorted(identities)}")
        self.ledger = ledger
        self.corpus = corpus
        self.root = Path(root)
        # The baseline runs first for every document.
        self.lanes = sorted(lanes, key=lambda lane: lane.name != baseline)
        self.baseline = baseline
        self.parser_config = dict(parser_config or {})
        self.work_dir = Path(work_dir) if work_dir else Path(journal).with_suffix(".work")
        self.keep_outputs = keep_outputs
        self._index = {entry.path: i for i, entry in enumerate(corpus.entries)}
        spec = self.lanes[0].spec
        self.journal = Journal(journal, header={
            "schema": JOURNAL_SCHEMA, "schema_version": JOURNAL_VERSION,
            "corpus_id": corpus.corpus_id, "corpus_name": corpus.name,
            "parser": {"name": spec.name, "version": spec.version},
            "parser_config_digest": config_digest(self.parser_config),
            "lanes": [{"name": lane.name, "backend": lane.backend.name} for lane in self.lanes],
            "baseline": baseline,
        })

    def pending(self) -> Iterator[tuple[str, Lane]]:
        done = self.journal.done()
        for entry in self.corpus.entries:
            for lane in self.lanes:
                if (entry.path, lane.name) not in done:
                    yield entry.path, lane

    def run(
        self,
        *,
        limit: int | None = None,
        progress: Callable[[Observation], None] | None = None,
    ) -> list[Observation]:
        """Observe every pending (document, lane); returns this call's observations."""
        for lane in self.lanes:
            if not lane.backend.available():
                raise CampaignError(
                    f"lane {lane.name}: backend {lane.backend.name} is unavailable here: "
                    f"{lane.backend.unavailable_reason()}"
                )
        observed: list[Observation] = []
        for path, lane in self.pending():
            if limit is not None and len(observed) >= limit:
                break
            observation = self.observe(path, lane)
            self.journal.append(observation)
            observed.append(observation)
            if progress is not None:
                progress(observation)
        return observed

    def observe(self, path: str, lane: Lane) -> Observation:
        number = self._index[path]
        entry = self.corpus.entries[number]
        document = self.root / path
        out = self.work_dir / lane.name / f"{number:08d}"
        if out.exists():
            shutil.rmtree(out)  # a leftover from an interrupted attempt
        out.parent.mkdir(parents=True, exist_ok=True)
        spec = lane.forced_spec()
        base = {"path": path, "lane": lane.name}
        try:
            spec.build_config(document, out, self.parser_config)
        except (ValueError, TypeError, KeyError) as exc:
            return Observation(**base, status=NOT_APPLICABLE, detail=str(exc))
        try:
            result = run_parser(
                spec, artifact_dir=out, parser_config=self.parser_config,
                input_path=document, store=self.ledger.archive,
            )
        except ContainmentCleanupError as exc:
            return Observation(**base, status=CONTAINMENT_ERROR, backend=lane.backend.name,
                               detail=str(exc))
        except UnsupportedBackendError as exc:
            return Observation(**base, status=ERROR, backend=lane.backend.name,
                               detail=f"backend refused the run: {exc}")
        except Exception as exc:  # noqa: BLE001 - every fault is an observation
            return Observation(**base, status=ERROR, backend=lane.backend.name,
                               detail=_one_line(exc))

        common = dict(
            base, backend=result.backend, run_id=str(result.run_id),
            input_sha256=result.input_sha256,
            telemetry=result.telemetry.to_dict() if result.telemetry is not None else None,
        )
        if result.input_sha256 != entry.sha256:
            _discard(out)
            return Observation(**common, status=ERROR, detail=(
                f"the parser read {result.input_sha256}, not the corpus document "
                f"{entry.sha256}: the file changed after the manifest was taken"
            ))
        if not result.succeeded or not result.produced_artifacts:
            failure = result.failure
            _discard(out)
            return Observation(
                **common, status=FAILED,
                failure=None if failure is None else {
                    "reason": failure.reason.value, "detail": failure.detail,
                    "exit_code": failure.exit_code, "signal": failure.signal,
                },
                detail=None if result.produced_artifacts else "the run produced no artifacts",
            )
        try:
            record = record_run(
                self.ledger, result, parser=spec.identity(),
                parser_config=self.parser_config, source=Source.from_path(document),
            )
        except Exception as exc:  # noqa: BLE001
            _discard(out)
            return Observation(**common, status=ERROR, detail=f"ledgering failed: {_one_line(exc)}")
        if not self.keep_outputs:
            _discard(out)
        return Observation(
            **common, status=SEALED, record_id=record.record_id,
            artifact_hash=record.artifact_hash,
        )


def _discard(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _one_line(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".splitlines()
    frame = traceback.extract_tb(exc.__traceback__)[-1:] if exc.__traceback__ else []
    where = f" (at {Path(frame[0].filename).name}:{frame[0].lineno})" if frame else ""
    return (text[0] if text else type(exc).__name__)[:500] + where

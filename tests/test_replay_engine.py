"""
Replay engine and cross-platform determinism harness (roadmap #14).

  1. A Wasm extractor replayed from a frozen Snapshot fixture is REPRODUCED,
     with the same digests on Linux, macOS and Windows (CI runs this file
     on all three).
  2. Injected non-determinism (unseeded entropy) is reported as DIVERGED,
     and feeds invalidation.
  3. An ML-style parser within its comparison policy is EQUIVALENT and never
     REPRODUCED, even when its bytes happen to match.
  4. A missing module, image, Snapshot, spec or backend is UNREPLAYABLE.
  5. Replays are recorded in an append-only replay log.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from stele.archive import Source
from stele.containment.backend import (
    BubblewrapBackend,
    Capability,
    ExecutionOutcome,
    ParserRequirements,
    SandboxBackend,
)
from stele.containment.oci import OciBackend
from stele.containment.sandbox import SandboxConfig
from stele.containment.wasm import WasmtimeBackend
from stele.contracts.adapter import LightRAGTarget, make_chunk
from stele.contracts.dispatcher import Dispatcher
from stele.extractors import CHATGPT_EXPORT_SPLIT_SPEC, EXTRACTOR_SPECS
from stele.ledger.models import ArtifactRecord, ArtifactState, ParserIdentity
from stele.ledger.store import SCHEMA_VERSION, LedgerStore
from stele.ledger.transaction import record_run
from stele.replay.engine import (
    ReplayLog,
    ReplayOutcome,
    current_platform,
    invalidate_diverged,
    replay_record,
)
from stele.replay.parsers import ParserCatalog, ParserSpec, run_parser
from stele.replay.policy import JsonTolerancePolicy
from tests.ledger_helpers import PROVENANCE, open_ledger

FIXTURES = Path(__file__).parent / "fixtures"
FROZEN_EXPORT = FIXTURES / "replay" / "conversations.json"
EXPECTED = json.loads((FIXTURES / "replay" / "chatgpt-export-split.expected.json").read_text())
PROBE = FIXTURES / "wasm" / "probe.wat"

pytestmark = pytest.mark.usefixtures("wasm_backend")


@pytest.fixture
def ledger(tmp_path: Path) -> LedgerStore:
    return open_ledger(tmp_path / "ledger.db")


def _record(
    ledger: LedgerStore, spec: ParserSpec, input_path: Path | None, tmp_path: Path,
    config: dict[str, Any] | None = None, name: str = "out",
) -> ArtifactRecord:
    config = config or {}
    run = run_parser(
        spec, artifact_dir=tmp_path / name, parser_config=config,
        input_path=input_path, store=ledger.archive,
    )
    assert run.succeeded, run.stderr
    source = Source.from_path(input_path) if input_path is not None else None
    return record_run(ledger, run, parser=spec.identity(), parser_config=config, source=source)


def _frozen_copy(tmp_path: Path) -> Path:
    """The frozen fixture, copied so the test can delete it after recording."""
    copy = tmp_path / "export" / "conversations.json"
    copy.parent.mkdir()
    copy.write_bytes(FROZEN_EXPORT.read_bytes())
    return copy


# ---------------------------------------------------------------------------
# 1 — The cross-platform harness
# ---------------------------------------------------------------------------

class TestFrozenSnapshotHarness:

    def test_wasm_extractor_is_reproduced_with_frozen_digests(self, ledger, tmp_path) -> None:
        source = _frozen_copy(tmp_path)
        record = _record(ledger, CHATGPT_EXPORT_SPLIT_SPEC, source, tmp_path)

        # The record itself matches the frozen expectation on this platform.
        assert record.source_hash == EXPECTED["input_sha256"]
        assert record.parser.module_sha256 == EXPECTED["parser"]["module_sha256"]
        assert record.artifact_manifest == EXPECTED["artifact_manifest"]
        assert record.artifact_hash == EXPECTED["artifact_hash"]

        # Replay works from the archive alone: the source and output are gone.
        source.unlink()
        shutil.rmtree(record.artifact_dir)
        result = replay_record(ledger, ParserCatalog(EXTRACTOR_SPECS), record)

        assert result.outcome is ReplayOutcome.REPRODUCED, result.reason
        assert result.replay_artifact_hash == EXPECTED["artifact_hash"]
        assert result.differences == ()
        assert result.backend == "wasmtime"
        assert result.platform == current_platform()
        assert result.replay_run_id is not None and result.replay_run_id != record.run_id

    def test_frozen_fixture_bytes_are_intact(self) -> None:
        import hashlib

        # Guards the harness itself: a line-ending rewrite on checkout would
        # change every digest above for the wrong reason.
        assert hashlib.sha256(FROZEN_EXPORT.read_bytes()).hexdigest() == EXPECTED["input_sha256"]

    def test_replay_is_logged(self, ledger, tmp_path) -> None:
        record = _record(ledger, CHATGPT_EXPORT_SPLIT_SPEC, _frozen_copy(tmp_path), tmp_path)
        catalog = ParserCatalog(EXTRACTOR_SPECS)
        first = replay_record(ledger, catalog, record.record_id)
        second = replay_record(ledger, catalog, record.record_id)

        log = ReplayLog(ledger)
        assert [r.replay_id for r in log.for_record(record.record_id)] == [
            first.replay_id, second.replay_id
        ]
        assert log.for_record(record.record_id)[0] == first
        assert len(log.with_outcome(ReplayOutcome.REPRODUCED)) == 2
        assert log.with_outcome(ReplayOutcome.DIVERGED) == []

        conn = sqlite3.connect(ledger.db_path)
        for statement in ("UPDATE replays SET outcome='reproduced'", "DELETE FROM replays"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                conn.execute(statement)
        conn.close()

    def test_only_records_with_sealed_evidence_are_replayed(self, ledger, tmp_path) -> None:
        out = tmp_path / "pending"
        out.mkdir()
        (out / "r.txt").write_text("x")
        pending = ledger.create_pending(
            **PROVENANCE, run_id="run-1", artifact_dir=out, artifact_paths=[out / "r.txt"]
        )
        with pytest.raises(ValueError, match="pending"):
            replay_record(ledger, ParserCatalog(), pending)


# ---------------------------------------------------------------------------
# 2 — Injected non-determinism
# ---------------------------------------------------------------------------

def _probe_spec(backend=None, module: Path = PROBE) -> ParserSpec:
    def build(input_path, artifact_dir, config):
        return SandboxConfig(
            command=[str(module), "d", input_path.name], artifact_dir=artifact_dir,
            input_path=input_path,
        )

    return ParserSpec(
        name="probe", version="1",
        requirements=ParserRequirements(wasm_module=True, deterministic=True),
        build_config=build, module_path=module, backend=backend,
    )


def _probe_input(tmp_path: Path) -> Path:
    path = tmp_path / "in" / "sample.txt"
    path.parent.mkdir()
    path.write_bytes(b"hello input\n")
    return path


class TestInjectedNondeterminism:

    def test_seeded_probe_is_reproduced(self, ledger, tmp_path) -> None:
        record = _record(ledger, _probe_spec(), _probe_input(tmp_path), tmp_path)
        result = replay_record(ledger, ParserCatalog([_probe_spec()]), record)
        assert result.outcome is ReplayOutcome.REPRODUCED, result.reason

    def test_unseeded_entropy_is_diverged_and_invalidated(self, ledger, tmp_path) -> None:
        record = _record(ledger, _probe_spec(), _probe_input(tmp_path), tmp_path)
        unseeded = _probe_spec(backend=lambda identity: WasmtimeBackend(entropy_seed=os.urandom(32)))

        result = replay_record(ledger, ParserCatalog([unseeded]), record)

        assert result.outcome is ReplayOutcome.DIVERGED
        assert result.differences == ("probe.bin",)
        assert result.replay_artifact_hash != record.artifact_hash
        # The divergent output is kept as evidence.
        assert ledger.archive.read_tree(result.replay_artifact_hash)

        # DIVERGED feeds invalidation (#13): the record's deliveries are removed.
        class Target:
            def __init__(self) -> None:
                self.rows: dict = {}

            def write_chunks(self, chunks, target, *, dispatch_id):
                for c in chunks:
                    self.rows[(dispatch_id, c.chunk_id)] = c.content

            def remove_delivery(self, target, *, dispatch_id):
                keys = [k for k in self.rows if k[0] == dispatch_id]
                for k in keys:
                    del self.rows[k]
                return len(keys)

        class Adapter:
            def transform(self, bundle):
                return [make_chunk(p, bundle.read(p).hex()) for p in bundle.paths()]

        target = Target()
        dispatcher = Dispatcher(ledger)
        dispatcher.register_target(LightRAGTarget, target)
        dispatcher.dispatch(Adapter(), record, LightRAGTarget("ws"))
        assert target.rows

        removals = invalidate_diverged(dispatcher, [result])
        assert [r.status for r in removals] == ["removed"]
        assert target.rows == {}
        assert ledger.get(record.record_id).state is ArtifactState.INVALIDATED
        assert "replay_diverged" in ledger.get(record.record_id).error

    def test_other_outcomes_do_not_invalidate(self, ledger, tmp_path) -> None:
        record = _record(ledger, _probe_spec(), _probe_input(tmp_path), tmp_path)
        result = replay_record(ledger, ParserCatalog([_probe_spec()]), record)
        assert invalidate_diverged(Dispatcher(ledger), [result]) == []
        assert ledger.get(record.record_id).state is ArtifactState.SEALED

    def test_failed_replay_run_is_diverged(self, ledger, tmp_path) -> None:
        record = _record(ledger, _probe_spec(), _probe_input(tmp_path), tmp_path)
        # Same module, but a CPU budget too small to finish: the run traps.
        tiny = _probe_spec(backend=lambda identity: WasmtimeBackend(fuel=10))
        result = replay_record(ledger, ParserCatalog([tiny]), record)
        assert result.outcome is ReplayOutcome.DIVERGED
        assert "failed" in result.reason


# ---------------------------------------------------------------------------
# 3 — ML parsers and comparison policies
# ---------------------------------------------------------------------------

class FakeMLBackend(SandboxBackend):
    """Stands in for a GPU parser: not deterministic, output set by knobs."""

    name = "fake-ml"

    def __init__(self, knobs: dict[str, Any]) -> None:
        self.knobs = knobs

    def capabilities(self):
        return frozenset({
            Capability.FILESYSTEM_ISOLATION, Capability.NETWORK_ISOLATION, Capability.HOST_PROCESS,
        })

    def available(self):
        return True

    def unavailable_reason(self):
        return ""

    def execute(self, config):
        out = config.artifact_dir
        out.mkdir(parents=True, exist_ok=True)
        (out / "layout.json").write_text(json.dumps({
            "score": self.knobs["score"],
            "generated_at": self.knobs["stamp"],
            "blocks": [{"text": "Title", "bbox": [0.0, 1.5, self.knobs["edge"], 9.0]}],
        }))
        (out / "text.md").write_text(self.knobs["text"])
        return ExecutionOutcome(exit_code=0, stdout="", stderr="", wall_time_seconds=0.0)


POLICY = JsonTolerancePolicy(abs_tol=0.01, ignore_keys=frozenset({"generated_at"}))
BASE_KNOBS = {"score": 0.91, "stamp": "2026-09-25T10:00:00Z", "edge": 4.0, "text": "# Title\n"}


def _ml_spec(knobs: dict[str, Any], policy=POLICY) -> ParserSpec:
    return ParserSpec(
        name="fake-ml", version="2.1",
        requirements=ParserRequirements(),
        build_config=lambda i, o, c: SandboxConfig(command=["ml"], artifact_dir=o, input_path=i),
        policy=policy,
        backend=lambda identity: FakeMLBackend(knobs),
    )


class TestComparisonPolicy:

    def _replay(self, ledger, tmp_path, **changes):
        knobs = dict(BASE_KNOBS)
        record = _record(ledger, _ml_spec(knobs), None, tmp_path)
        knobs.update(changes)
        return replay_record(ledger, ParserCatalog([_ml_spec(knobs)]), record)

    def test_within_policy_is_equivalent(self, ledger, tmp_path) -> None:
        result = self._replay(
            ledger, tmp_path, score=0.915, stamp="2026-09-25T11:00:00Z", edge=4.004
        )
        assert result.outcome is ReplayOutcome.EQUIVALENT
        assert "not proof of reproduction" in result.reason
        assert result.differences == ("layout.json",)
        assert result.policy == POLICY.describe() == {
            "name": "json-tolerance", "version": 1, "rel_tol": 0.0, "abs_tol": 0.01,
            "ignore_keys": ["generated_at"],
        }

    def test_identical_bytes_are_still_only_equivalent(self, ledger, tmp_path) -> None:
        result = self._replay(ledger, tmp_path)
        assert result.outcome is ReplayOutcome.EQUIVALENT
        assert result.differences == ()

    def test_beyond_tolerance_is_diverged(self, ledger, tmp_path) -> None:
        result = self._replay(ledger, tmp_path, edge=4.5)
        assert result.outcome is ReplayOutcome.DIVERGED
        assert result.differences == (
            "4.0 != 4.5 beyond tolerance: layout.json/blocks[0]/bbox[2]",
        )

    def test_non_json_difference_is_diverged(self, ledger, tmp_path) -> None:
        result = self._replay(ledger, tmp_path, text="# Titel\n")
        assert result.outcome is ReplayOutcome.DIVERGED
        assert result.differences == ("bytes differ: text.md",)

    def test_non_deterministic_parser_without_policy_is_unreplayable(self, ledger, tmp_path) -> None:
        knobs = dict(BASE_KNOBS)
        record = _record(ledger, _ml_spec(knobs, policy=None), None, tmp_path)
        result = replay_record(ledger, ParserCatalog([_ml_spec(knobs, policy=None)]), record)
        assert result.outcome is ReplayOutcome.UNREPLAYABLE
        assert "no comparison policy" in result.reason
        assert result.replay_run_id is None  # nothing was run


class TestJsonTolerancePolicyUnits:

    def _verdict(self, ledger, a: dict[str, bytes], b: dict[str, bytes], policy=POLICY):
        put = ledger.archive.put_bytes
        return policy.compare(
            {p: put(v) for p, v in a.items()}, {p: put(v) for p, v in b.items()}, ledger.archive
        )

    def test_file_sets_must_match(self, ledger) -> None:
        verdict = self._verdict(ledger, {"a.json": b"{}"}, {"b.json": b"{}"})
        assert verdict.findings == ("missing from replay: a.json", "not in the record: b.json")

    def test_jsonl_integers_and_types(self, ledger) -> None:
        verdict = self._verdict(
            ledger,
            {"x.jsonl": b'{"n": 1, "f": 1.0}\n{"s": "a"}\n'},
            {"x.jsonl": b'{"n": 2, "f": 1.001}\n{"s": 1}\n'},
        )
        assert verdict.findings == ("1 != 2: x.jsonl[0]/n", "'a' != 1: x.jsonl[1]/s")

    def test_negative_tolerance_is_refused(self) -> None:
        with pytest.raises(ValueError):
            JsonTolerancePolicy(abs_tol=-1)


# ---------------------------------------------------------------------------
# 4 — UNREPLAYABLE
# ---------------------------------------------------------------------------

class TestUnreplayable:

    def _split_record(self, ledger, tmp_path) -> ArtifactRecord:
        return _record(ledger, CHATGPT_EXPORT_SPLIT_SPEC, _frozen_copy(tmp_path), tmp_path)

    def _replay_with_module(self, ledger, record, module: Path):
        spec = ParserSpec(
            name=CHATGPT_EXPORT_SPLIT_SPEC.name, version=CHATGPT_EXPORT_SPLIT_SPEC.version,
            requirements=CHATGPT_EXPORT_SPLIT_SPEC.requirements,
            build_config=CHATGPT_EXPORT_SPLIT_SPEC.build_config, module_path=module,
        )
        return replay_record(ledger, ParserCatalog([spec]), record)

    def test_different_module_is_unreplayable(self, ledger, tmp_path) -> None:
        record = self._split_record(ledger, tmp_path)
        result = self._replay_with_module(ledger, record, PROBE)
        assert result.outcome is ReplayOutcome.UNREPLAYABLE
        assert "Wasm module" in result.reason and result.replay_run_id is None

    def test_missing_module_is_unreplayable(self, ledger, tmp_path) -> None:
        record = self._split_record(ledger, tmp_path)
        result = self._replay_with_module(ledger, record, tmp_path / "gone.wasm")
        assert result.outcome is ReplayOutcome.UNREPLAYABLE
        assert "Wasm module unavailable" in result.reason

    def test_missing_image_is_unreplayable(self, ledger, tmp_path) -> None:
        digest = "sha256:" + "0" * 64
        out = tmp_path / "ml-out"
        out.mkdir()
        (out / "layout.json").write_text("{}")
        record = ledger.seal(ledger.create_pending(
            parser=ParserIdentity("image-parser", "1", image_digest=digest),
            parser_config={}, run_id="image-run", artifact_dir=out,
            artifact_paths=[out / "layout.json"],
        ).record_id)
        spec = ParserSpec(
            name="image-parser", version="1", requirements=ParserRequirements(),
            build_config=lambda i, o, c: SandboxConfig(command=["parse"], artifact_dir=o),
            policy=POLICY,
            backend=lambda identity: OciBackend(image=f"stele-test/absent@{identity.image_digest}"),
        )
        result = replay_record(ledger, ParserCatalog([spec]), record)
        assert result.outcome is ReplayOutcome.UNREPLAYABLE
        assert "no capable backend" in result.reason

    def test_missing_snapshot_record_is_unreplayable(self, ledger, tmp_path) -> None:
        record = self._split_record(ledger, tmp_path)
        digest = record.source_hash
        (ledger.archive.root / "meta" / "snapshots" / "file" / digest[:2] / digest[2:]).unlink()
        result = replay_record(ledger, ParserCatalog(EXTRACTOR_SPECS), record)
        assert result.outcome is ReplayOutcome.UNREPLAYABLE
        assert "Snapshot" in result.reason

    def test_missing_snapshot_bytes_are_unreplayable(self, ledger, tmp_path) -> None:
        record = self._split_record(ledger, tmp_path)
        digest = record.source_hash
        blob = ledger.archive.root / "blobs" / digest[:2] / digest[2:]
        blob.chmod(0o600)
        blob.unlink()
        result = replay_record(ledger, ParserCatalog(EXTRACTOR_SPECS), record)
        assert result.outcome is ReplayOutcome.UNREPLAYABLE
        assert "Snapshot" in result.reason

    def test_unknown_parser_is_unreplayable(self, ledger, tmp_path) -> None:
        record = self._split_record(ledger, tmp_path)
        result = replay_record(ledger, ParserCatalog(), record)
        assert result.outcome is ReplayOutcome.UNREPLAYABLE
        assert "not in the catalog" in result.reason

    def test_no_capable_backend_is_unreplayable(self, ledger, tmp_path) -> None:
        record = _record(ledger, _probe_spec(), _probe_input(tmp_path), tmp_path)
        wrong = _probe_spec(backend=lambda identity: BubblewrapBackend())
        result = replay_record(ledger, ParserCatalog([wrong]), record)
        assert result.outcome is ReplayOutcome.UNREPLAYABLE
        assert "no capable backend" in result.reason

    def test_unreplayable_is_logged(self, ledger, tmp_path) -> None:
        record = self._split_record(ledger, tmp_path)
        result = replay_record(ledger, ParserCatalog(), record)
        assert ReplayLog(ledger).with_outcome(ReplayOutcome.UNREPLAYABLE) == [result]


# ---------------------------------------------------------------------------
# Catalog and schema
# ---------------------------------------------------------------------------

def test_catalog_refuses_duplicates() -> None:
    catalog = ParserCatalog(EXTRACTOR_SPECS)
    with pytest.raises(ValueError, match="already registered"):
        catalog.register(CHATGPT_EXPORT_SPLIT_SPEC)
    assert catalog.get("chatgpt-export-split", "1") is CHATGPT_EXPORT_SPLIT_SPEC
    assert catalog.get("chatgpt-export-split", "2") is None


def test_version_3_ledger_gains_the_replay_log(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    open_ledger(db).close()
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER replays_append_only_u")
    conn.execute("DROP TRIGGER replays_append_only_d")
    conn.execute("DROP TABLE replays")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    ledger = open_ledger(db)
    assert ReplayLog(ledger).all() == []
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
    conn.close()

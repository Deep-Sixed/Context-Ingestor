"""
The verification report: what a campaign proved, and whether it may be signed off.

build_report() trusts the journal only for what the ledger cannot know (why a
run failed, how long it took). Everything a journal line claims about a sealed
run is re-checked against the ledger and the evidence archive:

- the record exists, is still SEALED, and is the run the journal names
  (run id, artifact hash, parser name and version);
- its source_hash is the corpus manifest's SHA-256 for that document;
- its bundle re-verifies in the archive (validate_artifact);
- where the parser has an extraction normalizer, the canonical extraction
  normalizes and every unit resolves against the sealed bytes.

Then each candidate lane is compared with the baseline, document by document:

    both sealed    IDENTICAL (same manifest), EQUIVALENT (the parser's comparison
                   policy accepts the difference), or DIVERGED
    baseline only  REGRESSION: the candidate failed where production succeeds
    candidate only RECOVERED: the candidate succeeds where production fails
    both failed    CONSISTENT_FAILURE (same reason) or FAILURE_MISMATCH
    otherwise      NOT_APPLICABLE, INCOMPLETE (a lane has no observation) or ERROR

Gates decide sign-off. A comparison finding (DIVERGED, REGRESSION,
FAILURE_MISMATCH) may be waived per document and lane with a written reason,
and every waiver must match something. Integrity problems (a record that does
not verify, an unverifiable ledger, a changed corpus, a containment error)
are never waivable.

The report is canonical JSON (`stele.verification-report` v1); its SHA-256 is
what a sign-off (signoff.py) anchors in the ledger's event log.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..archive.records import canonical_json
from ..contracts.adapter import SealedBundle
from ..ledger.events import EventLog, verify_ledger
from ..ledger.models import ArtifactState
from ..ledger.store import LedgerStore, RecordNotFoundError
from ..replay.policy import ComparisonPolicy
from ..replay.validator import validate_artifact
from .campaign import CONTAINMENT_ERROR, ERROR, FAILED, NOT_APPLICABLE, SEALED, Journal, Observation
from .corpus import Corpus

REPORT_SCHEMA = "stele.verification-report"
REPORT_VERSION = 1

IDENTICAL = "identical"
EQUIVALENT = "equivalent"
DIVERGED = "diverged"
REGRESSION = "regression"
RECOVERED = "recovered"
CONSISTENT_FAILURE = "consistent_failure"
FAILURE_MISMATCH = "failure_mismatch"
INCOMPLETE = "incomplete"
# NOT_APPLICABLE and ERROR share the observation status names.

WAIVABLE = frozenset({DIVERGED, REGRESSION, FAILURE_MISMATCH})
_MAX_FINDINGS = 20


class ReportFormatError(ValueError):
    """Bytes that are not a valid stele.verification-report/v1."""


@dataclass(frozen=True)
class Waiver:
    """An accepted comparison finding: one document, one candidate lane, one outcome."""

    path: str
    lane: str
    outcome: str
    reason: str

    def __post_init__(self) -> None:
        if self.outcome not in WAIVABLE:
            raise ValueError(
                f"only {sorted(WAIVABLE)} can be waived, not {self.outcome!r}; integrity "
                "problems must be fixed"
            )
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("a waiver needs a written reason")

    def to_json(self) -> dict[str, str]:
        return {"path": self.path, "lane": self.lane, "outcome": self.outcome, "reason": self.reason}

    @classmethod
    def load_all(cls, path: Path) -> list["Waiver"]:
        data = json.loads(Path(path).read_text())
        if not isinstance(data, list):
            raise ValueError(f"{path}: waivers must be a JSON list")
        return [cls(**w) for w in data]


@dataclass(frozen=True)
class Gates:
    """Thresholds beyond the fixed gates. None disables a gate."""

    # Share of applicable documents the baseline may fail (0.01 = 1%).
    max_baseline_failure_rate: float | None = None
    # Median candidate/baseline wall-time ratio over documents both sealed.
    max_slowdown: float | None = None
    # Verify the canonical extraction of every sealed record that has a normalizer.
    check_extractions: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "max_baseline_failure_rate": self.max_baseline_failure_rate,
            "max_slowdown": self.max_slowdown,
            "check_extractions": self.check_extractions,
        }


@dataclass(frozen=True)
class VerificationReport:
    data: dict[str, Any]
    _canonical: bytes = field(repr=False, compare=False, default=b"")

    def __post_init__(self) -> None:
        if not self._canonical:
            object.__setattr__(self, "_canonical", canonical_json(self.data))

    def to_canonical(self) -> bytes:
        return self._canonical

    @property
    def digest(self) -> str:
        return hashlib.sha256(self._canonical).hexdigest()

    @property
    def passed(self) -> bool:
        return bool(self.data["passed"])

    @property
    def corpus_id(self) -> str:
        return self.data["corpus"]["id"]

    @property
    def ledger_head(self) -> tuple[int, str] | None:
        head = self.data["ledger"]["head"]
        return None if head is None else (head["seq"], head["hash"])

    def failed_gates(self) -> list[dict[str, Any]]:
        return [g for g in self.data["gates"] if not g["passed"]]

    def record_ids(self) -> list[str]:
        ids = []
        for doc in self.data["documents"]:
            for lane in doc["lanes"].values():
                if lane.get("record_id"):
                    ids.append(lane["record_id"])
        return ids

    @classmethod
    def from_canonical(cls, data: bytes) -> "VerificationReport":
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ReportFormatError(f"not UTF-8 JSON: {exc}") from exc
        if not isinstance(obj, dict) or obj.get("schema") != REPORT_SCHEMA \
                or obj.get("schema_version") != REPORT_VERSION:
            raise ReportFormatError(f"expected {REPORT_SCHEMA} v{REPORT_VERSION}")
        for key in ("corpus", "parser", "lanes", "baseline", "ledger", "documents", "summary",
                    "gates", "waivers", "passed"):
            if key not in obj:
                raise ReportFormatError(f"report has no {key!r}")
        if canonical_json(obj) != data:
            raise ReportFormatError("report is not in canonical form")
        return cls(obj, data)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def build_report(
    ledger: LedgerStore,
    corpus: Corpus,
    journal: Journal,
    *,
    root: Path | None,
    policy: ComparisonPolicy | None = None,
    gates: Gates = Gates(),
    waivers: Sequence[Waiver] = (),
) -> VerificationReport:
    """Re-check a campaign against the ledger and decide every gate.

    root is the corpus directory, re-hashed against the manifest; without it
    the corpus_unchanged gate fails (a report that has not checked the corpus
    cannot be signed off). policy is the parser's comparison policy (the same
    one replays are judged by); without one, any difference is DIVERGED.
    """
    header = journal.header
    if header["corpus_id"] != corpus.corpus_id:
        raise ValueError("the journal is of another corpus")
    lanes = [lane["name"] for lane in header["lanes"]]
    baseline = header["baseline"]
    candidates = [name for name in lanes if name != baseline]
    parser = header["parser"]

    by_doc: dict[str, dict[str, Observation]] = {}
    for obs in journal.observations:
        by_doc.setdefault(obs.path, {})[obs.lane] = obs   # the latest observation wins

    checker = _RecordChecker(ledger, parser, gates.check_extractions)
    documents: list[dict[str, Any]] = []
    waiver_index = {(w.path, w.lane, w.outcome): w for w in waivers}
    used: set[tuple[str, str, str]] = set()
    outcome_counts: dict[str, Counter] = {c: Counter() for c in candidates}
    unwaived: list[str] = []
    integrity: list[str] = []
    errors: list[str] = []
    missing = 0
    slowdowns: dict[str, list[float]] = {c: [] for c in candidates}

    for entry in corpus.entries:
        observed = by_doc.get(entry.path, {})
        lane_rows: dict[str, dict[str, Any]] = {}
        problems: list[str] = []
        records: dict[str, Any] = {}
        for name in lanes:
            obs = observed.get(name)
            if obs is None:
                missing += 1
                lane_rows[name] = {"status": INCOMPLETE}
                continue
            row = _lane_row(obs)
            if obs.status in (ERROR, CONTAINMENT_ERROR):
                errors.append(f"{entry.path} [{name}]: {obs.status}: {obs.detail}")
            if obs.status == SEALED:
                record, lane_problems, units = checker.check(obs, entry.sha256)
                problems += [f"[{name}] {p}" for p in lane_problems]
                if units is not None:
                    row["extraction_units"] = units
                if record is not None:
                    records[name] = record
            lane_rows[name] = row
        integrity += [f"{entry.path} {p}" for p in problems]

        comparisons: dict[str, dict[str, Any]] = {}
        base = observed.get(baseline)
        for cand in candidates:
            outcome, findings = _compare(
                base, observed.get(cand), records.get(baseline), records.get(cand),
                policy, ledger,
            )
            row: dict[str, Any] = {"outcome": outcome}
            if findings:
                row["findings"] = findings[:_MAX_FINDINGS]
                if len(findings) > _MAX_FINDINGS:
                    row["findings_truncated"] = len(findings) - _MAX_FINDINGS
            key = (entry.path, cand, outcome)
            if outcome in WAIVABLE:
                if key in waiver_index:
                    used.add(key)
                    row["waived"] = waiver_index[key].reason
                else:
                    unwaived.append(f"{entry.path} [{cand}]: {outcome}")
            outcome_counts[cand][outcome] += 1
            comparisons[cand] = row
            if outcome in (IDENTICAL, EQUIVALENT):
                ratio = _ratio(base, observed.get(cand))
                if ratio is not None:
                    slowdowns[cand].append(ratio)

        doc: dict[str, Any] = {
            "path": entry.path, "sha256": entry.sha256, "size": entry.size,
            "lanes": lane_rows, "comparisons": comparisons,
        }
        if problems:
            doc["problems"] = problems
        documents.append(doc)

    ledger_report = verify_ledger(ledger)
    log = EventLog(ledger)
    try:
        head = log.head()
    finally:
        log.close()

    summary = _summary(corpus, journal, lanes, candidates, outcome_counts, slowdowns)
    gate_rows = _gates(
        corpus=corpus, root=root, lanes=lanes, baseline=baseline, missing=missing,
        errors=errors, integrity=integrity, ledger_report=ledger_report,
        unwaived=unwaived, unused=[w for k, w in waiver_index.items() if k not in used],
        summary=summary, gates=gates, slowdowns=slowdowns,
    )
    data = {
        "schema": REPORT_SCHEMA,
        "schema_version": REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus": {
            "id": corpus.corpus_id, "name": corpus.name, "documents": len(corpus),
            "bytes": corpus.total_bytes, "excluded": [x.to_json() for x in corpus.excluded],
        },
        "parser": parser,
        "parser_config_digest": header["parser_config_digest"],
        "lanes": header["lanes"],
        "baseline": baseline,
        "policy": policy.describe() if policy is not None else None,
        "ledger": {
            "head": None if head is None else {"seq": head[0], "hash": head[1]},
            "verified": ledger_report.ok,
        },
        "thresholds": gates.to_json(),
        "waivers": [w.to_json() for w in waivers],
        "documents": documents,
        "summary": summary,
        "gates": gate_rows,
        "passed": all(g["passed"] for g in gate_rows),
    }
    return VerificationReport(data)


def _lane_row(obs: Observation) -> dict[str, Any]:
    row: dict[str, Any] = {"status": obs.status}
    for key in ("backend", "record_id", "artifact_hash"):
        if getattr(obs, key) is not None:
            row[key] = getattr(obs, key)
    if obs.failure is not None:
        row["failure"] = {"reason": obs.failure.get("reason"), "detail": obs.failure.get("detail")}
    if obs.detail:
        row["detail"] = obs.detail
    t = obs.telemetry or {}
    for key in ("wall_time_seconds", "cpu_time_seconds", "peak_memory_bytes"):
        if t.get(key) is not None:
            row[key] = t[key]
    return row


class _RecordChecker:
    def __init__(self, ledger: LedgerStore, parser: Mapping[str, str], extractions: bool) -> None:
        self.ledger = ledger
        self.parser = parser
        self.extractions = extractions

    def check(self, obs: Observation, sha256: str) -> tuple[Any, list[str], int | None]:
        """(record or None, problems, extraction unit count or None)."""
        try:
            record = self.ledger.get(obs.record_id)
        except RecordNotFoundError:
            return None, [f"record {obs.record_id} is not in the ledger"], None
        problems = []
        if record.state is not ArtifactState.SEALED:
            problems.append(f"record {record.record_id} is {record.state.value}, not sealed")
        if record.run_id != obs.run_id or record.artifact_hash != obs.artifact_hash:
            problems.append(f"record {record.record_id} is not the run the journal names")
        if record.parser is None or (record.parser.name, record.parser.version) != (
            self.parser["name"], self.parser["version"]
        ):
            problems.append(f"record {record.record_id} was made by another parser")
        if record.source_hash != sha256:
            problems.append(
                f"record {record.record_id} read input {record.source_hash}, not the corpus document"
            )
        validation = validate_artifact(record, self.ledger.archive)
        if validation.status not in ("ok", "archived"):
            problems.append(
                f"record {record.record_id} does not verify: {validation.status} "
                f"{list(validation.drifted_files or validation.missing_files)[:5]}"
            )
            return record, problems, None
        units = None
        if self.extractions:
            units, extraction_problems = _check_extraction(record, self.ledger)
            problems += extraction_problems
        return record, problems, units


def _check_extraction(record: Any, ledger: LedgerStore) -> tuple[int | None, list[str]]:
    from ..extraction.normalizers import NormalizeError, normalizer_for
    from ..extraction.resolver import BundleResolver

    bundle = SealedBundle.from_record(record, ledger.archive)
    try:
        normalizer = normalizer_for(bundle)
    except NormalizeError:
        return None, []          # no normalizer for this parser: nothing to check
    try:
        extraction = normalizer(bundle)
    except Exception as exc:  # noqa: BLE001 - a normalizer failure is a finding
        return None, [f"record {record.record_id}: extraction failed: {exc}"]
    problems = BundleResolver(bundle).verify(extraction)
    return len(extraction.units), [
        f"record {record.record_id}: extraction: {p}" for p in problems[:_MAX_FINDINGS]
    ]


def _compare(
    base: Observation | None,
    cand: Observation | None,
    base_record: Any,
    cand_record: Any,
    policy: ComparisonPolicy | None,
    ledger: LedgerStore,
) -> tuple[str, list[str]]:
    if base is None or cand is None:
        return INCOMPLETE, []
    statuses = {base.status, cand.status}
    if statuses & {ERROR, CONTAINMENT_ERROR}:
        return ERROR, []
    if base.status == NOT_APPLICABLE and cand.status == NOT_APPLICABLE:
        return NOT_APPLICABLE, []
    if NOT_APPLICABLE in statuses:
        # One lane accepted the document and the other did not: the lanes
        # do not agree on what they run.
        return FAILURE_MISMATCH, ["the parser accepted the document on one lane only"]
    if base.status == SEALED and cand.status == SEALED:
        if base_record is None or cand_record is None:
            return ERROR, ["a sealed record is missing from the ledger"]
        recorded, candidate = base_record.artifact_manifest, cand_record.artifact_manifest
        if recorded == candidate:
            return IDENTICAL, []
        if policy is not None:
            verdict = policy.compare(recorded, candidate, ledger.archive)
            if verdict.accepted:
                return EQUIVALENT, []
            return DIVERGED, list(verdict.findings)
        differing = sorted(
            p for p in set(recorded) | set(candidate) if recorded.get(p) != candidate.get(p)
        )
        return DIVERGED, [f"differs: {p}" for p in differing]
    if base.status == SEALED:
        return REGRESSION, [_failure_text(cand)]
    if cand.status == SEALED:
        return RECOVERED, [_failure_text(base)]
    if _reason(base) == _reason(cand):
        return CONSISTENT_FAILURE, [_failure_text(base)]
    return FAILURE_MISMATCH, [f"baseline {_failure_text(base)}; candidate {_failure_text(cand)}"]


def _reason(obs: Observation) -> str | None:
    return (obs.failure or {}).get("reason")


def _failure_text(obs: Observation) -> str:
    failure = obs.failure or {}
    return f"{failure.get('reason', 'failed')}: {failure.get('detail') or obs.detail or ''}".strip()


def _wall(obs: Observation | None) -> float | None:
    if obs is None or obs.telemetry is None:
        return None
    return obs.telemetry.get("wall_time_seconds")


def _ratio(base: Observation | None, cand: Observation | None) -> float | None:
    b, c = _wall(base), _wall(cand)
    if b is None or c is None or b <= 0:
        return None
    return c / b


# ---------------------------------------------------------------------------
# Summary and gates
# ---------------------------------------------------------------------------

def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(q * len(ordered)) - 1)
    return round(ordered[index], 6)


def _stats(values: Iterable[float | None]) -> dict[str, Any]:
    vals = [v for v in values if v is not None]
    return {
        "n": len(vals), "p50": _percentile(vals, 0.5), "p95": _percentile(vals, 0.95),
        "max": round(max(vals), 6) if vals else None,
    }


def _summary(
    corpus: Corpus,
    journal: Journal,
    lanes: list[str],
    candidates: list[str],
    outcome_counts: dict[str, Counter],
    slowdowns: dict[str, list[float]],
) -> dict[str, Any]:
    latest: dict[tuple[str, str], Observation] = {}
    wanted = corpus.by_path()
    for obs in journal.observations:
        if obs.path in wanted:
            latest[(obs.path, obs.lane)] = obs
    per_lane: dict[str, Any] = {}
    for name in lanes:
        observations = [o for (p, lane), o in latest.items() if lane == name]
        statuses = Counter(o.status for o in observations)
        reasons = Counter(_reason(o) or "unknown" for o in observations if o.status == FAILED)
        per_lane[name] = {
            "observed": len(observations),
            "statuses": dict(sorted(statuses.items())),
            "failure_reasons": dict(sorted(reasons.items())),
            "wall_time_seconds": _stats((o.telemetry or {}).get("wall_time_seconds")
                                        for o in observations),
            "cpu_time_seconds": _stats((o.telemetry or {}).get("cpu_time_seconds")
                                       for o in observations),
            "peak_memory_bytes": _stats((o.telemetry or {}).get("peak_memory_bytes")
                                        for o in observations),
        }
    return {
        "lanes": per_lane,
        "comparisons": {
            cand: {
                "outcomes": dict(sorted(outcome_counts[cand].items())),
                "median_slowdown": (
                    round(statistics.median(slowdowns[cand]), 6) if slowdowns[cand] else None
                ),
            }
            for cand in candidates
        },
    }


def _gate(name: str, passed: bool, detail: str, examples: Sequence[str] = ()) -> dict[str, Any]:
    row: dict[str, Any] = {"name": name, "passed": bool(passed), "detail": detail}
    if examples:
        row["examples"] = list(examples[:_MAX_FINDINGS])
    return row


def _gates(
    *,
    corpus: Corpus,
    root: Path | None,
    lanes: list[str],
    baseline: str,
    missing: int,
    errors: list[str],
    integrity: list[str],
    ledger_report: Any,
    unwaived: list[str],
    unused: list[Waiver],
    summary: dict[str, Any],
    gates: Gates,
    slowdowns: dict[str, list[float]],
) -> list[dict[str, Any]]:
    rows = []
    if root is None:
        rows.append(_gate("corpus_unchanged", False, "the corpus was not re-hashed (no root given)"))
    else:
        changed = corpus.check(root)
        rows.append(_gate(
            "corpus_unchanged", not changed,
            "every document still hashes as in the manifest" if not changed
            else f"{len(changed)} difference(s) from the manifest", changed,
        ))
    expected = len(corpus) * len(lanes)
    rows.append(_gate(
        "coverage", missing == 0 and len(corpus) > 0,
        f"{expected - missing}/{expected} document-lane runs observed"
        + ("" if len(corpus) else "; the corpus is empty"),
    ))
    base_sealed = summary["lanes"][baseline]["statuses"].get(SEALED, 0)
    rows.append(_gate(
        "baseline_produced_output", base_sealed > 0,
        f"the baseline sealed {base_sealed} document(s)",
    ))
    rows.append(_gate(
        "no_errors", not errors,
        "no Stele, host or containment errors" if not errors
        else f"{len(errors)} run(s) ended in an error", errors,
    ))
    rows.append(_gate(
        "records_verified", not integrity,
        "every sealed record verifies against the ledger, archive and corpus" if not integrity
        else f"{len(integrity)} integrity problem(s)", integrity,
    ))
    rows.append(_gate(
        "ledger_verified", ledger_report.ok,
        "the event chain and the ledger tables agree" if ledger_report.ok
        else "the ledger does not verify",
        [*ledger_report.chain.problems, *ledger_report.problems],
    ))
    rows.append(_gate(
        "candidates_agree", not unwaived,
        "every candidate agrees with the baseline or is waived" if not unwaived
        else f"{len(unwaived)} unwaived finding(s)", unwaived,
    ))
    rows.append(_gate(
        "waivers_used", not unused,
        "every waiver matches a finding" if not unused
        else f"{len(unused)} waiver(s) match nothing",
        [f"{w.path} [{w.lane}]: {w.outcome}" for w in unused],
    ))
    if gates.max_baseline_failure_rate is not None:
        statuses = summary["lanes"][baseline]["statuses"]
        applicable = sum(n for s, n in statuses.items() if s != NOT_APPLICABLE)
        rate = statuses.get(FAILED, 0) / applicable if applicable else 0.0
        rows.append(_gate(
            "baseline_failure_rate", rate <= gates.max_baseline_failure_rate,
            f"{rate:.4%} of applicable documents failed on the baseline "
            f"(limit {gates.max_baseline_failure_rate:.4%})",
        ))
    if gates.max_slowdown is not None:
        slow = [
            f"{cand}: median {statistics.median(v):.2f}x" for cand, v in slowdowns.items()
            if v and statistics.median(v) > gates.max_slowdown
        ]
        rows.append(_gate(
            "slowdown", not slow,
            f"median candidate/baseline wall time within {gates.max_slowdown}x" if not slow
            else f"{len(slow)} lane(s) slower than {gates.max_slowdown}x", slow,
        ))
    return rows

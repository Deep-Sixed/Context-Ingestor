"""
Sign-off: a person accepting a passed verification report, on the record.

sign_off() does not take the report's word for anything. It rebuilds the
report from the ledger, the journal and the corpus (with the report's own
thresholds and waivers) and refuses unless the result is the same report, so
an edited report, with its gates flipped to passed and re-encoded, is
refused. It also refuses unless every gate passed, the ledger's chain still
contains the head the report was built at (nothing was rewritten since), and
every record the report relies on is still SEALED with the artifact hash it
names. It then stores the report's canonical bytes
in the evidence archive and appends a `verification.signed_off` event to the
hash-chained event log, whose body names the report digest, the corpus id,
the parser, the lanes and who signed off. The event log is what makes the
sign-off tamper-evident; publishing the resulting head (seq, hash) outside
the ledger makes it hold against a rewritten ledger too.

check_sign_off() re-verifies a sign-off later: the event exists and is
chained, the archived report still hashes to its digest, and every record it
covers is still sealed. A sign-off whose records were since invalidated is
reported as stale, never silently kept.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..archive.store import ArchiveError
from ..ledger.events import EventLog, append_event, write_transaction
from ..ledger.models import ArtifactState
from ..ledger.store import LedgerStore, RecordNotFoundError, connect
from ..replay.policy import ComparisonPolicy
from .campaign import Journal
from .corpus import Corpus
from .report import Gates, ReportFormatError, VerificationReport, Waiver, build_report

SIGNOFF_EVENT = "verification.signed_off"


class SignOffRefused(Exception):
    """The report cannot be signed off; .problems says why."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems[:5]) + (" ..." if len(problems) > 5 else ""))
        self.problems = problems


@dataclass(frozen=True)
class SignOff:
    report_digest: str
    corpus_id: str
    signed_off_by: str
    seq: int          # the event's position in the chain
    event_hash: str   # publish (seq, event_hash) to anchor the sign-off


def recheck(ledger: LedgerStore, report: VerificationReport) -> list[str]:
    """Why the report no longer describes the ledger; [] if it still does."""
    problems: list[str] = []
    log = EventLog(ledger)
    try:
        chain = log.verify(anchor=report.ledger_head)
    finally:
        log.close()
    problems += [f"event log: {p}" for p in chain.problems]
    for doc in report.data["documents"]:
        for lane, row in doc["lanes"].items():
            record_id = row.get("record_id")
            if not record_id:
                continue
            try:
                record = ledger.get(record_id)
            except RecordNotFoundError:
                problems.append(f"{doc['path']} [{lane}]: record {record_id} is gone")
                continue
            if record.state is not ArtifactState.SEALED:
                problems.append(
                    f"{doc['path']} [{lane}]: record {record_id} is now {record.state.value}"
                )
            elif record.artifact_hash != row.get("artifact_hash"):
                problems.append(f"{doc['path']} [{lane}]: record {record_id} changed")
    return problems


def _comparable(data: dict[str, Any]) -> dict[str, Any]:
    """The report without what legitimately differs between two builds of it."""
    return {
        **data, "generated_at": None,
        "ledger": {**data["ledger"], "head": None},
    }


def rebuild_problems(
    ledger: LedgerStore,
    report: VerificationReport,
    *,
    corpus: Corpus,
    journal: Journal,
    root: Path,
    policy: ComparisonPolicy | None = None,
) -> list[str]:
    """[] if building the report again from its inputs gives the same report."""
    thresholds = report.data["thresholds"]
    try:
        again = build_report(
            ledger, corpus, journal, root=root, policy=policy,
            gates=Gates(**thresholds),
            waivers=[Waiver(**w) for w in report.data["waivers"]],
        )
    except (TypeError, ValueError) as exc:
        return [f"the report cannot be rebuilt from its inputs: {exc}"]
    if _comparable(again.data) == _comparable(report.data):
        return []
    differing = sorted(
        k for k in set(again.data) | set(report.data)
        if _comparable(again.data).get(k) != _comparable(report.data).get(k)
    )
    return [f"the report does not match a rebuild from the ledger, journal and corpus "
            f"(differs in: {', '.join(differing)})"]


def sign_off(
    ledger: LedgerStore,
    report: VerificationReport,
    *,
    corpus: Corpus,
    journal: Journal,
    root: Path,
    signed_off_by: str,
    policy: ComparisonPolicy | None = None,
    note: str = "",
) -> SignOff:
    if not isinstance(signed_off_by, str) or not signed_off_by.strip():
        raise ValueError("a sign-off names the person who gives it")
    problems = rebuild_problems(
        ledger, report, corpus=corpus, journal=journal, root=root, policy=policy
    )
    problems += [
        f"gate {g['name']} failed: {g['detail']}" for g in report.failed_gates()
    ]
    if not report.data["ledger"]["verified"]:
        problems.append("the report was built on a ledger that did not verify")
    problems += recheck(ledger, report)
    if problems:
        raise SignOffRefused(problems)

    digest = ledger.archive.put_bytes(report.to_canonical())
    if digest != report.digest:
        raise RuntimeError("the archive stored the report under another digest")
    data = report.data
    body = {
        "report_digest": digest,
        "corpus_id": data["corpus"]["id"],
        "corpus_name": data["corpus"]["name"],
        "documents": data["corpus"]["documents"],
        "parser": data["parser"],
        "lanes": data["lanes"],
        "baseline": data["baseline"],
        "report_ledger_head": data["ledger"]["head"],
        "waivers": len(data["waivers"]),
        "signed_off_by": signed_off_by.strip(),
        "note": note,
    }
    conn = connect(ledger.db_path)
    try:
        with write_transaction(conn):
            event = append_event(conn, SIGNOFF_EVENT, data["corpus"]["id"], body)
    finally:
        conn.close()
    return SignOff(digest, data["corpus"]["id"], body["signed_off_by"], event.seq, event.hash)


def sign_offs(ledger: LedgerStore) -> list[dict]:
    """Every sign-off event: [{seq, hash, at, **body}], oldest first."""
    log = EventLog(ledger)
    try:
        return [
            {"seq": e.seq, "hash": e.hash, "at": e.at, **e.body}
            for e in log.events() if e.kind == SIGNOFF_EVENT
        ]
    finally:
        log.close()


def check_sign_off(ledger: LedgerStore, report_digest: str) -> list[str]:
    """Problems with a recorded sign-off; [] means it still holds."""
    matches = [s for s in sign_offs(ledger) if s.get("report_digest") == report_digest]
    if not matches:
        return [f"no sign-off of report {report_digest} in the event log"]
    log = EventLog(ledger)
    try:
        chain = log.verify(anchor=(matches[-1]["seq"], matches[-1]["hash"]))
    finally:
        log.close()
    problems = [f"event log: {p}" for p in chain.problems]
    try:
        report = VerificationReport.from_canonical(ledger.archive.read(report_digest))
    except (ArchiveError, ReportFormatError) as exc:
        return problems + [f"the signed-off report is unreadable: {exc}"]
    if not report.passed:
        problems.append("the signed-off report did not pass")
    problems += [f"stale: {p}" for p in recheck(ledger, report)]
    return problems

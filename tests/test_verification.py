"""
Full-corpus production verification and sign-off (roadmap #15).

  1. A corpus is pinned by content: every regular file hashed, everything left
     out listed with its reason; strict canonical manifest; check() catches a
     changed, missing or added document.
  2. A campaign runs every document on the baseline and each candidate lane,
     seals successful runs, journals every observation (failures with their
     structured reason) and resumes where it stopped.
  3. The report re-checks every sealed record against the ledger, archive,
     corpus and extraction resolver, compares candidates with the baseline
     (IDENTICAL, EQUIVALENT under the parser's policy, DIVERGED, REGRESSION,
     RECOVERED, CONSISTENT_FAILURE, FAILURE_MISMATCH) and decides the gates.
     Only comparison findings can be waived, each with a reason, and a waiver
     that matches nothing fails the gates.
  4. Sign-off is refused unless every gate passes and the report still holds;
     it is anchored in the event log and goes stale when a record it covers
     is invalidated.
  5. End to end with real sandboxes (bubblewrap baseline; OCI candidate when
     an engine is available) and through the CLI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

import pytest

from stele.containment.backend import (
    Capability,
    ExecutionOutcome,
    ParserRequirements,
    SandboxBackend,
)
from stele.containment.sandbox import SandboxConfig
from stele.ledger.events import EventLog, verify_ledger
from stele.replay.parsers import ParserSpec
from stele.replay.policy import JsonTolerancePolicy
from stele.verification import (
    SIGNOFF_EVENT,
    Campaign,
    CampaignError,
    Corpus,
    CorpusFormatError,
    Gates,
    Journal,
    Lane,
    SignOffRefused,
    VerificationReport,
    Waiver,
    build_report,
    check_sign_off,
    sign_off,
)
from stele.verification.report import (
    CONSISTENT_FAILURE,
    DIVERGED,
    EQUIVALENT,
    IDENTICAL,
    RECOVERED,
    REGRESSION,
)
from tests.ledger_helpers import open_ledger

# ---------------------------------------------------------------------------
# Fixtures: a corpus and in-process lanes
# ---------------------------------------------------------------------------

DOCS = {
    "a.txt": b"alpha document\n",
    "nested/b.txt": b"bravo document\n",
    "nested/deeper/c.txt": "charlie — ünïcode\n".encode(),
    "fail.txt": b"FAIL: this document breaks the parser\n",
}


def _corpus_dir(tmp_path: Path, docs: dict[str, bytes] = DOCS) -> Path:
    root = tmp_path / "corpus"
    for rel, data in docs.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


# (document bytes) -> (exit code, {relative path: bytes})
Behaviour = Callable[[bytes], tuple[int, dict[str, bytes]]]


def honest(data: bytes) -> tuple[int, dict[str, bytes]]:
    if data.startswith(b"FAIL"):
        return 3, {}
    words = data.decode().split()
    return 0, {"out.json": json.dumps({"words": words, "score": 0.5}).encode()}


class InProcessBackend(SandboxBackend):
    """A lane whose 'parser' is a Python function (no sandbox: tests only)."""

    def __init__(self, name: str, behaviour: Behaviour) -> None:
        self.name = name
        self.behaviour = behaviour

    def capabilities(self):
        return frozenset(Capability)

    def available(self):
        return True

    def unavailable_reason(self):
        return ""

    def execute(self, config: SandboxConfig) -> ExecutionOutcome:
        code, files = self.behaviour(config.input_path.read_bytes())
        for rel, data in files.items():
            (config.artifact_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (config.artifact_dir / rel).write_bytes(data)
        return ExecutionOutcome(exit_code=code, stdout="", stderr="", wall_time_seconds=0.01)


def _spec(name: str = "probe", version: str = "1") -> ParserSpec:
    def build(input_path, artifact_dir, config):
        if input_path.suffix == ".bin":
            raise ValueError("probe does not accept .bin")
        return SandboxConfig(command=["probe"], artifact_dir=artifact_dir, input_path=input_path)

    return ParserSpec(name=name, version=version, requirements=ParserRequirements(),
                      build_config=build)


def _lane(name: str, behaviour: Behaviour, spec: ParserSpec | None = None) -> Lane:
    return Lane(name, InProcessBackend(name, behaviour), spec or _spec())


def _campaign(tmp_path, lanes, *, root=None, ledger=None, journal="journal.jsonl", **kw):
    root = root or _corpus_dir(tmp_path)
    ledger = ledger or open_ledger(tmp_path / "ledger.db")
    corpus = Corpus.build(root)
    campaign = Campaign(ledger, corpus, root, lanes, baseline=lanes[0].name,
                        journal=tmp_path / journal, **kw)
    return campaign, ledger, corpus, root


def _report(campaign, ledger, corpus, root, **kw) -> VerificationReport:
    return build_report(ledger, corpus, campaign.journal, root=root, **kw)


def _gate(report: VerificationReport, name: str) -> dict:
    return next(g for g in report.data["gates"] if g["name"] == name)


def _doc(report: VerificationReport, path: str) -> dict:
    return next(d for d in report.data["documents"] if d["path"] == path)


# ---------------------------------------------------------------------------
# 1 — the corpus
# ---------------------------------------------------------------------------

class TestCorpus:

    def test_build_hashes_every_regular_file(self, tmp_path) -> None:
        root = _corpus_dir(tmp_path)
        corpus = Corpus.build(root, name="prod")
        assert corpus.name == "prod"
        assert [e.path for e in corpus.entries] == sorted(DOCS)
        assert corpus.by_path()["a.txt"].size == len(DOCS["a.txt"])
        assert corpus.total_bytes == sum(len(d) for d in DOCS.values())
        assert corpus.excluded == ()
        assert corpus.check(root) == []

    @pytest.mark.skipif(not hasattr(os, "mkfifo"),
                        reason="Windows feature: no FIFOs or unprivileged symlinks")
    def test_everything_left_out_is_listed(self, tmp_path) -> None:
        root = _corpus_dir(tmp_path)
        os.symlink(root / "a.txt", root / "link.txt")
        os.symlink(root / "nested", root / "linked-dir")
        os.mkfifo(root / "pipe")
        corpus = Corpus.build(root)
        assert [e.path for e in corpus.entries] == sorted(DOCS)
        assert [(x.path, x.why) for x in corpus.excluded] == [
            ("link.txt", "symlink"), ("linked-dir", "symlink"), ("pipe", "not a regular file"),
        ]
        assert corpus.check(root) == []

    def test_canonical_round_trip_and_strictness(self, tmp_path) -> None:
        corpus = Corpus.build(_corpus_dir(tmp_path))
        data = corpus.to_canonical()
        assert Corpus.from_canonical(data) == corpus
        assert Corpus.from_canonical(data).corpus_id == corpus.corpus_id
        pretty = json.dumps(json.loads(data), indent=1).encode()
        with pytest.raises(CorpusFormatError, match="canonical"):
            Corpus.from_canonical(pretty)
        obj = json.loads(data)
        obj["entries"][0]["path"] = "../escape"
        with pytest.raises(CorpusFormatError, match="relative"):
            Corpus.from_canonical(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode())

    def test_check_catches_changed_missing_and_added(self, tmp_path) -> None:
        root = _corpus_dir(tmp_path)
        corpus = Corpus.build(root)
        (root / "a.txt").write_bytes(b"alpha, edited\n")
        (root / "nested" / "b.txt").unlink()
        (root / "new.txt").write_bytes(b"new")
        assert sorted(corpus.check(root)) == [
            "a.txt: content changed since the manifest was taken",
            "nested/b.txt: missing",
            "new.txt: not in the corpus manifest",
        ]


# ---------------------------------------------------------------------------
# 2 — the campaign
# ---------------------------------------------------------------------------

class TestCampaign:

    def test_every_document_on_every_lane(self, tmp_path) -> None:
        campaign, ledger, corpus, _ = _campaign(
            tmp_path, [_lane("base", honest), _lane("cand", honest)]
        )
        observed = campaign.run()
        assert len(observed) == 2 * len(DOCS)
        by = {(o.path, o.lane): o for o in observed}
        sealed = by[("a.txt", "base")]
        assert sealed.status == "sealed"
        record = ledger.get(sealed.record_id)
        assert record.source_hash == corpus.by_path()["a.txt"].sha256
        assert record.parser.name == "probe" and record.backend == "base"
        failed = by[("fail.txt", "cand")]
        assert failed.status == "failed" and failed.record_id is None
        assert failed.failure["reason"] == "exit_status" and failed.failure["exit_code"] == 3
        assert failed.telemetry["wall_time_seconds"] is not None
        # Working outputs are dropped once sealed; the archive holds them.
        assert not any(p.is_file() for p in campaign.work_dir.rglob("*"))

    def test_resumes_where_it_stopped(self, tmp_path) -> None:
        lanes = [_lane("base", honest), _lane("cand", honest)]
        campaign, ledger, corpus, root = _campaign(tmp_path, lanes)
        assert len(campaign.run(limit=3)) == 3
        again = Campaign(ledger, corpus, root, lanes, baseline="base",
                         journal=tmp_path / "journal.jsonl")
        assert len(again.journal.observations) == 3
        rest = again.run()
        assert len(rest) == 2 * len(DOCS) - 3
        assert not list(again.pending())
        assert len(Journal.read(tmp_path / "journal.jsonl").observations) == 2 * len(DOCS)

    def test_torn_last_line_is_dropped(self, tmp_path) -> None:
        lanes = [_lane("base", honest)]
        campaign, ledger, corpus, root = _campaign(tmp_path, lanes)
        campaign.run(limit=2)
        with open(tmp_path / "journal.jsonl", "ab") as f:
            f.write(b'{"path":"nested/b.txt","la')     # a crash mid-append
        again = Campaign(ledger, corpus, root, lanes, baseline="base",
                         journal=tmp_path / "journal.jsonl")
        assert len(again.journal.observations) == 2
        again.run()
        assert len(Journal.read(tmp_path / "journal.jsonl").observations) == len(DOCS)

    def test_journal_of_another_campaign_is_refused(self, tmp_path) -> None:
        campaign, ledger, corpus, root = _campaign(tmp_path, [_lane("base", honest)])
        campaign.run(limit=1)
        with pytest.raises(CampaignError, match="another campaign"):
            Campaign(ledger, corpus, root, [_lane("base", honest)], baseline="base",
                     journal=tmp_path / "journal.jsonl", parser_config={"mode": "fast"})

    def test_lanes_must_run_one_parser(self, tmp_path) -> None:
        with pytest.raises(CampaignError, match="same parser"):
            _campaign(tmp_path, [_lane("base", honest), _lane("cand", honest, _spec(version="2"))])

    def test_not_applicable_documents(self, tmp_path) -> None:
        docs = {**DOCS, "blob.bin": b"\x00\x01"}
        campaign, *_ = _campaign(tmp_path, [_lane("base", honest)],
                                 root=_corpus_dir(tmp_path, docs))
        by = {o.path: o for o in campaign.run()}
        assert by["blob.bin"].status == "not_applicable"
        assert "does not accept" in by["blob.bin"].detail

    def test_a_document_changed_mid_campaign_is_an_error(self, tmp_path) -> None:
        campaign, _, _, root = _campaign(tmp_path, [_lane("base", honest)])
        (root / "a.txt").write_bytes(b"swapped after the manifest\n")
        by = {o.path: o for o in campaign.run()}
        assert by["a.txt"].status == "error" and "changed after the manifest" in by["a.txt"].detail


# ---------------------------------------------------------------------------
# 3 — the report and its gates
# ---------------------------------------------------------------------------

def noisy(data: bytes):
    """Same words, a score that moved by a tolerable amount."""
    code, files = honest(data)
    if code:
        return code, files
    obj = json.loads(files["out.json"])
    obj["score"] = 0.5000001
    return 0, {"out.json": json.dumps(obj).encode()}


def wrong(data: bytes):
    code, files = honest(data)
    if code:
        return code, files
    return 0, {"out.json": json.dumps({"words": ["something", "else"], "score": 0.5}).encode()}


def flaky(data: bytes):
    """Fails on 'bravo'; succeeds where the baseline fails."""
    if b"bravo" in data:
        return 1, {}
    if data.startswith(b"FAIL"):
        return 0, {"out.json": b"{}"}
    return honest(data)


class TestReport:

    def test_clean_campaign_passes_every_gate(self, tmp_path) -> None:
        campaign, ledger, corpus, root = _campaign(tmp_path, [_lane("base", honest),
                                                              _lane("cand", honest)])
        campaign.run()
        report = _report(campaign, ledger, corpus, root)
        assert report.passed, report.failed_gates()
        assert [g["name"] for g in report.data["gates"]] == [
            "corpus_unchanged", "coverage", "baseline_produced_output", "no_errors",
            "records_verified", "ledger_verified", "candidates_agree", "waivers_used",
        ]
        assert _doc(report, "a.txt")["comparisons"]["cand"]["outcome"] == IDENTICAL
        assert _doc(report, "fail.txt")["comparisons"]["cand"]["outcome"] == CONSISTENT_FAILURE
        lane = report.data["summary"]["lanes"]["base"]
        assert lane["statuses"] == {"failed": 1, "sealed": 3}
        assert lane["failure_reasons"] == {"exit_status": 1}
        assert lane["wall_time_seconds"]["n"] == 4
        again = VerificationReport.from_canonical(report.to_canonical())
        assert again.digest == report.digest and again.passed

    def test_outcomes_against_the_baseline(self, tmp_path) -> None:
        campaign, ledger, corpus, root = _campaign(tmp_path, [
            _lane("base", honest), _lane("noisy", noisy), _lane("wrong", wrong),
            _lane("flaky", flaky),
        ])
        campaign.run()
        policy = JsonTolerancePolicy(abs_tol=1e-3)
        report = _report(campaign, ledger, corpus, root, policy=policy)
        a = _doc(report, "a.txt")["comparisons"]
        assert a["noisy"]["outcome"] == EQUIVALENT
        assert a["wrong"]["outcome"] == DIVERGED and a["wrong"]["findings"]
        assert _doc(report, "nested/b.txt")["comparisons"]["flaky"]["outcome"] == REGRESSION
        assert _doc(report, "fail.txt")["comparisons"]["flaky"]["outcome"] == RECOVERED
        assert not _gate(report, "candidates_agree")["passed"]
        assert report.data["policy"] == policy.describe()
        # Without the parser's policy, the tolerable difference is a divergence too.
        strict = _report(campaign, ledger, corpus, root)
        assert _doc(strict, "a.txt")["comparisons"]["noisy"]["outcome"] == DIVERGED

    def test_waivers_accept_findings_and_must_all_match(self, tmp_path) -> None:
        campaign, ledger, corpus, root = _campaign(tmp_path, [_lane("base", honest),
                                                              _lane("flaky", flaky)])
        campaign.run()
        waiver = Waiver("nested/b.txt", "flaky", REGRESSION, "known upstream bug #123")
        report = _report(campaign, ledger, corpus, root, waivers=[waiver])
        assert report.passed, report.failed_gates()
        assert _doc(report, "nested/b.txt")["comparisons"]["flaky"]["waived"] == waiver.reason
        unused = Waiver("a.txt", "flaky", DIVERGED, "no longer needed")
        report = _report(campaign, ledger, corpus, root, waivers=[waiver, unused])
        assert not _gate(report, "waivers_used")["passed"]
        with pytest.raises(ValueError, match="integrity"):
            Waiver("a.txt", "flaky", "error", "hide it")
        with pytest.raises(ValueError, match="reason"):
            Waiver("a.txt", "flaky", REGRESSION, " ")

    def test_incomplete_campaign_fails_coverage(self, tmp_path) -> None:
        campaign, ledger, corpus, root = _campaign(tmp_path, [_lane("base", honest),
                                                              _lane("cand", honest)])
        campaign.run(limit=5)
        report = _report(campaign, ledger, corpus, root)
        assert not _gate(report, "coverage")["passed"]
        assert "5/8" in _gate(report, "coverage")["detail"]

    def test_integrity_problems_fail_the_report(self, tmp_path) -> None:
        campaign, ledger, corpus, root = _campaign(tmp_path, [_lane("base", honest)])
        campaign.run()
        record_id = next(o.record_id for o in campaign.journal.observations if o.path == "a.txt")
        ledger.invalidate(record_id, "manual")
        report = _report(campaign, ledger, corpus, root)
        gate = _gate(report, "records_verified")
        assert not gate["passed"] and "invalidated" in gate["examples"][0]

    def test_tampered_evidence_fails_the_report(self, tmp_path) -> None:
        campaign, ledger, corpus, root = _campaign(tmp_path, [_lane("base", honest)])
        campaign.run()
        record = ledger.get(campaign.journal.observations[0].record_id)
        blob = ledger.archive._blob_path(record.artifact_manifest["out.json"])
        blob.chmod(0o644)
        blob.write_bytes(b'{"words":[],"score":9}')
        gate = _gate(_report(campaign, ledger, corpus, root), "records_verified")
        assert not gate["passed"] and "does not verify" in gate["examples"][0]

    def test_changed_or_unchecked_corpus_fails(self, tmp_path) -> None:
        campaign, ledger, corpus, root = _campaign(tmp_path, [_lane("base", honest)])
        campaign.run()
        assert not _gate(_report(campaign, ledger, corpus, None), "corpus_unchanged")["passed"]
        (root / "extra.txt").write_bytes(b"late arrival")
        assert not _gate(_report(campaign, ledger, corpus, root), "corpus_unchanged")["passed"]

    def test_errors_and_thresholds(self, tmp_path) -> None:
        def broken(data):
            raise RuntimeError("host fault")

        campaign, ledger, corpus, root = _campaign(tmp_path, [_lane("base", honest),
                                                              _lane("cand", broken)])
        campaign.run()
        report = _report(campaign, ledger, corpus, root,
                         gates=Gates(max_baseline_failure_rate=0.2, max_slowdown=100))
        assert not _gate(report, "no_errors")["passed"]
        assert "host fault" in _gate(report, "no_errors")["examples"][0]
        rate = _gate(report, "baseline_failure_rate")
        assert not rate["passed"] and "25.0000%" in rate["detail"]

    def test_extractions_are_verified(self, tmp_path) -> None:
        def markdown(data: bytes):
            if data.startswith(b"FAIL"):
                return 3, {}
            return 0, {
                "document.md": b"# Title\n\n" + data,
                "stele-parser.json": json.dumps({"outputs": {"markdown": "document.md"}}).encode(),
            }

        def bad_markdown(data: bytes):
            code, files = markdown(data)
            if code == 0 and b"bravo" in data:
                files["document.md"] = b"caf\xe9\n"
            return code, files

        spec = _spec(name="marker", version="test")
        campaign, ledger, corpus, root = _campaign(
            tmp_path, [_lane("base", markdown, spec), _lane("cand", bad_markdown, spec)]
        )
        campaign.run()
        report = _report(campaign, ledger, corpus, root)
        assert _doc(report, "a.txt")["lanes"]["base"]["extraction_units"] == 2
        gate = _gate(report, "records_verified")
        assert not gate["passed"] and "extraction failed" in gate["examples"][0]


# ---------------------------------------------------------------------------
# 4 — sign-off
# ---------------------------------------------------------------------------

class TestSignOff:

    def _passed(self, tmp_path):
        campaign, ledger, corpus, root = _campaign(tmp_path, [_lane("base", honest),
                                                              _lane("cand", honest)])
        campaign.run()
        report = _report(campaign, ledger, corpus, root)
        assert report.passed
        self.inputs = {"corpus": corpus, "journal": campaign.journal, "root": root}
        return ledger, report

    def test_sign_off_is_anchored_and_checkable(self, tmp_path) -> None:
        ledger, report = self._passed(tmp_path)
        done = sign_off(ledger, report, **self.inputs, signed_off_by="C. Snyder",
                        note="Q3 production")
        assert done.report_digest == report.digest
        log = EventLog(ledger)
        [event] = [e for e in log.events() if e.kind == SIGNOFF_EVENT]
        log.close()
        assert (event.seq, event.hash) == (done.seq, done.event_hash)
        assert event.body["report_digest"] == report.digest
        assert event.body["signed_off_by"] == "C. Snyder"
        assert event.body["corpus_id"] == report.corpus_id
        assert ledger.archive.read(report.digest) == report.to_canonical()
        assert check_sign_off(ledger, report.digest) == []
        assert verify_ledger(ledger).ok

    def test_failed_report_is_refused(self, tmp_path) -> None:
        campaign, ledger, corpus, root = _campaign(tmp_path, [_lane("base", honest),
                                                              _lane("wrong", wrong)])
        campaign.run()
        with pytest.raises(SignOffRefused, match="candidates_agree"):
            sign_off(ledger, _report(campaign, ledger, corpus, root), corpus=corpus,
                     journal=campaign.journal, root=root, signed_off_by="x")

    def test_report_that_no_longer_holds_is_refused(self, tmp_path) -> None:
        ledger, report = self._passed(tmp_path)
        ledger.invalidate(report.record_ids()[0], "found a problem")
        with pytest.raises(SignOffRefused, match="does not match a rebuild") as refused:
            sign_off(ledger, report, **self.inputs, signed_off_by="x")
        assert any("now invalidated" in p for p in refused.value.problems)

    def test_edited_report_is_refused(self, tmp_path) -> None:
        campaign, ledger, corpus, root = _campaign(tmp_path, [_lane("base", honest),
                                                              _lane("wrong", wrong)])
        campaign.run()
        report = _report(campaign, ledger, corpus, root)
        assert not report.passed
        obj = json.loads(report.to_canonical())
        obj["passed"] = True
        for gate in obj["gates"]:
            gate["passed"] = True
        with pytest.raises(Exception, match="canonical"):
            VerificationReport.from_canonical(json.dumps(obj, indent=1).encode())
        # Re-encoded canonically it parses, but it is not what the evidence says.
        forged = VerificationReport.from_canonical(
            json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        )
        assert forged.passed
        with pytest.raises(SignOffRefused, match="does not match a rebuild"):
            sign_off(ledger, forged, corpus=corpus, journal=campaign.journal, root=root,
                     signed_off_by="x")

    def test_invalidation_makes_a_sign_off_stale(self, tmp_path) -> None:
        ledger, report = self._passed(tmp_path)
        sign_off(ledger, report, **self.inputs, signed_off_by="x")
        ledger.invalidate(report.record_ids()[0], "source_changed")
        problems = check_sign_off(ledger, report.digest)
        assert problems and all(p.startswith("stale:") for p in problems)
        assert check_sign_off(ledger, "0" * 64) == [f"no sign-off of report {'0' * 64} in the event log"]


# ---------------------------------------------------------------------------
# 5 — real sandboxes and the CLI
# ---------------------------------------------------------------------------

PARSER_SCRIPT = '''
import hashlib, json, os, sys
data = open(os.environ["STELE_INPUT_PATH"], "rb").read()
if data.startswith(b"FAIL"):
    sys.exit(3)
out = os.environ.get("STELE_OUTPUT_DIR", "/stele/output")
with open(os.path.join(out, "summary.json"), "w") as f:
    json.dump({"sha256": hashlib.sha256(data).hexdigest(), "lines": data.count(b"\\n")}, f,
              sort_keys=True)
'''


def _script(tmp_path: Path) -> Path:
    path = tmp_path / "summarize.py"
    path.write_text(PARSER_SCRIPT)
    return path


def _require(backend_name: str):
    from stele.verification.lanes import backend_named

    backend = backend_named(backend_name)
    if not backend.available():
        pytest.skip(f"{backend_name} unavailable: {backend.unavailable_reason()}")


class TestRealSandboxes:

    def test_bubblewrap_baseline_end_to_end(self, tmp_path) -> None:
        from stele.verification.lanes import script_lanes

        _require("bubblewrap")
        lanes = script_lanes(_script(tmp_path), ["bubblewrap"], deterministic=False)
        campaign, ledger, corpus, root = _campaign(tmp_path, lanes)
        campaign.run()
        report = _report(campaign, ledger, corpus, root)
        assert report.passed, report.failed_gates()
        statuses = report.data["summary"]["lanes"]["bubblewrap"]["statuses"]
        assert statuses == {"failed": 1, "sealed": 3}
        sealed = ledger.get(_doc(report, "a.txt")["lanes"]["bubblewrap"]["record_id"])
        summary = json.loads(ledger.archive.read(sealed.artifact_manifest["summary.json"]))
        assert summary["sha256"] == corpus.by_path()["a.txt"].sha256
        assert sealed.parser.version.startswith("sha256-")

    def test_oci_candidate_against_the_bubblewrap_baseline(self, tmp_path) -> None:
        from stele.verification.lanes import script_lanes

        _require("bubblewrap")
        _require("oci-runc")
        lanes = script_lanes(_script(tmp_path), ["bubblewrap", "oci-runc"])
        campaign, ledger, corpus, root = _campaign(tmp_path, lanes)
        campaign.run()
        report = _report(campaign, ledger, corpus, root)
        assert report.passed, report.failed_gates()
        outcomes = report.data["summary"]["comparisons"]["oci-runc"]["outcomes"]
        assert outcomes == {IDENTICAL: 3, CONSISTENT_FAILURE: 1}

    def test_cli_corpus_run_report_sign_off_check(self, tmp_path, capsys) -> None:
        from stele.verification.__main__ import main

        _require("bubblewrap")
        root = _corpus_dir(tmp_path)
        store = ["--ledger", str(tmp_path / "ledger.db"), "--archive", str(tmp_path / "archive")]
        manifest, journal, out = tmp_path / "corpus.json", tmp_path / "j.jsonl", tmp_path / "r.json"
        campaign = [*store, "--corpus", str(manifest), "--root", str(root), "--journal", str(journal)]
        assert main(["corpus", str(root), "-o", str(manifest)]) == 0
        assert json.loads(capsys.readouterr().out)["documents"] == len(DOCS)
        assert main(["run", *campaign, "--script", str(_script(tmp_path)), "--quiet",
                     "--limit", "2"]) == 0
        assert json.loads(capsys.readouterr().out) == {"observed": 2, "remaining": 2}
        assert main(["report", *campaign, "-o", str(out)]) == 1          # incomplete
        assert "FAIL  coverage" in capsys.readouterr().out
        assert main(["run", *campaign, "--script", str(_script(tmp_path)), "--quiet"]) == 0
        capsys.readouterr()
        assert main(["report", *campaign, "-o", str(out)]) == 0
        digest = VerificationReport.from_canonical(out.read_bytes()).digest
        capsys.readouterr()
        assert main(["sign-off", *campaign, str(out), "--by", "operator"]) == 0
        signed = json.loads(capsys.readouterr().out)
        assert signed["report_digest"] == digest and ":" in signed["anchor"]
        assert main(["check", *store, digest]) == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True
        assert main(["list", *store]) == 0
        assert json.loads(capsys.readouterr().out)[0]["signed_off_by"] == "operator"


def test_unavailable_lane_refuses_to_start(tmp_path) -> None:
    class Missing(InProcessBackend):
        def available(self):
            return False

        def unavailable_reason(self):
            return "not installed"

    campaign, *_ = _campaign(tmp_path, [_lane("base", honest),
                                        Lane("gone", Missing("gone", honest), _spec())])
    with pytest.raises(CampaignError, match="not installed"):
        campaign.run()

"""
Roadmap #9/#10 — the real parser images, end to end.

These tests need the parser images built locally (the trusted build step), so
they are marked `parser_image` and deselected by default. The parser-images
workflow builds each image and runs:

    STELE_PARSER_IMAGES=mineru STELE_PARSER_ENGINE=docker \\
    STELE_PARSER_NOMODEL_IMAGE=localhost/stele/mineru:nomodels \\
        pytest -m parser_image tests/test_parser_images.py

For every parser named in STELE_PARSER_IMAGES:

  - each representative document it accepts parses end to end inside the
    container, and the extraction contains the document's marker phrases;
  - the output follows the documented layout (stele-parser.json manifest
    naming the parser, version, configuration and output roles);
  - the run's identity records the image digest and configuration digest;
  - an image without model weights fails cleanly with exit status 3, without
    trying to download anything, and keeps no output;
  - a recorded run replayed on the same image is EQUIVALENT under the
    parser's comparison policy, never REPRODUCED (roadmap #14).

The containment proofs (tests/test_phase_e_containment.py) run against the
same images via STELE_PROOF_IMAGES.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from stele.archive.store import BlobStore
from stele.containment.oci import OciBackend
from stele.parsers import config_digest, run_parser
from stele.parsers.catalog import get_parser

pytestmark = pytest.mark.parser_image

DOCS = Path(__file__).parent / "fixtures" / "documents"
sys.path.insert(0, str(DOCS))
from make_fixtures import MARKERS  # noqa: E402

NAMES = [n for n in os.environ.get("STELE_PARSER_IMAGES", "").split(",") if n]
ENGINE = os.environ.get("STELE_PARSER_ENGINE") or None
NOMODEL_IMAGE = os.environ.get("STELE_PARSER_NOMODEL_IMAGE") or None

# Parser-specific phrases OCR may not reproduce exactly are matched loosely:
# case, whitespace and markdown emphasis are ignored.
_NOISE = re.compile(r"[\s*_#|`\\]+")


def _norm(text: str) -> str:
    return _NOISE.sub(" ", text).lower()


# Documents a parser accepts but is not expected to extract, with the reason
# its run must fail with instead.
REFUSED = {
    ("marker", "scanned.pdf"): "no text extracted",  # no OCR model in the image
}


def _cases() -> list[tuple[str, str]]:
    cases = []
    for name in NAMES:
        parser = get_parser(name)
        for doc in sorted(MARKERS):
            if Path(doc).suffix in parser.formats and (name, doc) not in REFUSED:
                cases.append((name, doc))
    return cases



def _cpu_backend(name: str, image: str | None = None) -> OciBackend:
    parser = get_parser(name)
    backend = OciBackend(
        image=image or parser.image, engine=ENGINE, memory=parser.memory,
        cpus=parser.cpus, pids_limit=parser.pids_limit, tmpfs_size=parser.tmpfs_size,
    )
    assert backend.available(), backend.unavailable_reason()
    return backend


@pytest.mark.parametrize(("name", "doc"), _cases(), ids=[f"{n}-{d}" for n, d in _cases()])
def test_document_parses_end_to_end(name: str, doc: str, tmp_path: Path) -> None:
    parser = get_parser(name)
    store = BlobStore(tmp_path / "store")
    out = tmp_path / "out"
    run = run_parser(parser, DOCS / doc, out, store=store, backend=_cpu_backend(name))

    assert run.succeeded, f"{run.failure}\n--- stderr ---\n{run.result.stderr[-4000:]}"

    manifest = json.loads((out / "stele-parser.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "stele.parser-output/v1"
    assert manifest["parser"] == name
    assert manifest["version"] == parser.version
    assert manifest["config"] == dict(parser.config)
    assert manifest["input"] == doc
    for role, rel in manifest["outputs"].items():
        assert (out / rel).is_file(), (role, rel)

    markdown = (out / manifest["outputs"]["markdown"]).read_text(encoding="utf-8")
    text = _norm(markdown)
    missing = [m for m in MARKERS[doc] if _norm(m) not in text]
    assert not missing, f"missing {missing} in:\n{markdown[:3000]}"

    # Everything the parser wrote was captured and stored by digest.
    assert set(run.result.artifact_digests) == {
        p.relative_to(out).as_posix() for p in run.result.artifact_paths
    }
    assert "stele-parser.json" in run.result.artifact_digests
    assert run.result.artifact_bundle_digest is not None

    ident = run.identity
    assert ident.image_digest and ident.image_digest.startswith("sha256:")
    assert ident.config_sha256 == config_digest(parser.config)
    assert ident.device == "cpu"


@pytest.mark.parametrize("name", NAMES)
def test_image_without_models_fails_cleanly(name: str, tmp_path: Path) -> None:
    if NOMODEL_IMAGE is None:
        pytest.fail("set STELE_PARSER_NOMODEL_IMAGE to an image built with SKIP_MODELS=1")
    parser = get_parser(name)
    doc = next(d for d in sorted(MARKERS) if Path(d).suffix in parser.formats)
    out = tmp_path / "out"
    run = run_parser(
        replace(parser, image=NOMODEL_IMAGE), DOCS / doc, out,
        backend=_cpu_backend(name, NOMODEL_IMAGE), timeout_seconds=300,
    )
    assert not run.succeeded
    assert run.result.exit_code == 3, run.result.stderr[-4000:]
    assert "model weights missing" in run.failure
    assert not run.result.timed_out  # refused at once, no download attempts
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("name", NAMES)
def test_bad_configuration_is_refused(name: str, tmp_path: Path) -> None:
    parser = get_parser(name)
    doc = next(d for d in sorted(MARKERS) if Path(d).suffix in parser.formats)
    run = run_parser(parser, DOCS / doc, tmp_path / "out", config={"no_such_option": 1},
                     backend=_cpu_backend(name))
    assert run.result.exit_code == 2, run.result.stderr[-4000:]
    assert "unknown configuration keys: no_such_option" in run.failure


@pytest.mark.parametrize("name", NAMES)
def test_unsupported_documents_are_clean_failures(name: str, tmp_path: Path) -> None:
    """Documents a parser accepts but cannot extract fail cleanly, keeping nothing."""
    for (parser_name, doc), reason in REFUSED.items():
        if parser_name != name:
            continue
        out = tmp_path / doc
        run = run_parser(get_parser(name), DOCS / doc, out, backend=_cpu_backend(name))
        assert not run.succeeded
        assert reason in run.failure, run.result.stderr[-4000:]
        assert list(out.iterdir()) == []


@pytest.mark.parametrize("name", NAMES)
def test_memory_limit_is_a_clean_failure(name: str, tmp_path: Path) -> None:
    """A parser that outgrows its memory limit is killed; nothing is kept."""
    parser = get_parser(name)
    doc = next(d for d in sorted(MARKERS) if Path(d).suffix == ".pdf")
    store = BlobStore(tmp_path / "store")
    out = tmp_path / "out"
    backend = OciBackend(
        image=parser.image, engine=ENGINE, memory="200m", cpus=parser.cpus,
        pids_limit=parser.pids_limit, tmpfs_size="64m",
    )
    run = run_parser(parser, DOCS / doc, out, store=store, backend=backend)
    assert not run.succeeded
    # Killed at the limit (137), or the parser caught MemoryError (4).
    assert run.result.exit_code in (137, 4), (run.result.exit_code, run.result.stderr[-4000:])
    assert list(out.iterdir()) == []
    assert run.result.artifact_bundle_digest is None


@pytest.mark.parametrize("name", NAMES)
def test_time_limit_is_a_clean_failure(name: str, tmp_path: Path) -> None:
    parser = get_parser(name)
    doc = next(d for d in sorted(MARKERS) if Path(d).suffix == ".pdf")
    out = tmp_path / "out"
    run = run_parser(parser, DOCS / doc, out, backend=_cpu_backend(name), timeout_seconds=3)
    assert run.result.timed_out and "timed out after 3s" in run.failure
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("name", NAMES)
def test_replay_is_equivalent_never_reproduced(name: str, tmp_path: Path) -> None:
    """Roadmap #14: a real ML parser replayed on its recorded Snapshot, with its
    recorded config and image, lands within its comparison policy."""
    from stele.ledger.store import LedgerStore
    from stele.parsers.replay import record_parser_run, replay_spec
    from stele.replay.engine import ReplayOutcome, replay_record
    from stele.replay.parsers import ParserCatalog

    parser = get_parser(name)
    doc = "text_only.pdf"
    ledger = LedgerStore(tmp_path / "ledger.db", BlobStore(tmp_path / "archive"))
    run = run_parser(parser, DOCS / doc, tmp_path / "out", store=ledger.archive,
                     backend=_cpu_backend(name))
    assert run.succeeded, f"{run.failure}\n--- stderr ---\n{run.result.stderr[-4000:]}"
    record = record_parser_run(ledger, run)

    spec = replay_spec(parser, backend=_cpu_backend(name))
    result = replay_record(ledger, ParserCatalog([spec]), record)

    assert result.outcome is ReplayOutcome.EQUIVALENT, (result.reason, result.differences)
    assert result.policy == parser.comparison.describe()

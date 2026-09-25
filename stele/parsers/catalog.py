"""The packaged parsers Stele knows how to build and run."""
from __future__ import annotations

from ..replay.policy import JsonTolerancePolicy
from . import ParserImage

# Replay policy for the packaged ML parsers (roadmap #14). The parsers run on
# the CPU with capped thread pools, but BLAS/onnxruntime reduction order can
# still move a float in its last digits between runs. Coordinates are in PDF
# points or pixels: half a unit absorbs that noise, anything larger is a real
# layout change. Text (Markdown, strings in JSON) must match exactly, and so
# must every other file. "device" in stele-parser.json is where the run
# happened, not what it extracted.
ML_REPLAY_POLICY = JsonTolerancePolicy(
    rel_tol=1e-6, abs_tol=0.5, ignore_keys=frozenset({"device"}),
)

MINERU = ParserImage(
    name="mineru",
    version="4.0.7",
    image="localhost/stele/mineru:4.0.7",
    containerfile="parsers/mineru/Containerfile",
    config={"tier": "basic", "ocr_mode": "auto", "image_analysis": True, "pages": ""},
    formats=(".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"),
    # A GPU is used when the host can pass one through and the GPU image
    # (parsers/mineru/Containerfile.gpu) has been built; otherwise the CPU
    # image runs. The GPU image is untested on real hardware (no GPU in CI).
    gpu="optional",
    gpu_image="localhost/stele/mineru-gpu:4.0.7",
    memory="6g",
    cpus=2.0,
    tmpfs_size="2g",
    timeout_seconds=1800,
    comparison=ML_REPLAY_POLICY,
)

MARKER = ParserImage(
    name="marker",
    version="2.0.0",
    image="localhost/stele/marker:2.0.0",
    containerfile="parsers/marker/Containerfile",
    # Fast, text-layer mode: CPU layout/table detectors, no VLM OCR model in
    # the image, so scans are refused rather than returned empty.
    config={"mode": "fast", "disable_ocr": True, "extract_images": True},
    formats=(".pdf", ".docx", ".pptx", ".xlsx", ".html", ".epub"),
    gpu="never",
    memory="6g",
    cpus=2.0,
    # Marker runs its layout and OCR-error models in local helper servers
    # (loopback only; the sandbox has no other network).
    pids_limit=2048,
    tmpfs_size="2g",
    timeout_seconds=1800,
    comparison=ML_REPLAY_POLICY,
)

DOCLING = ParserImage(
    name="docling",
    version="2.130.0",
    image="localhost/stele/docling:2.130.0",
    containerfile="parsers/docling/Containerfile",
    config={"do_ocr": True, "force_full_page_ocr": False, "do_table_structure": True},
    formats=(".pdf", ".docx", ".pptx", ".xlsx", ".html", ".md", ".png", ".jpg", ".jpeg", ".tiff", ".tif"),
    gpu="never",
    memory="6g",
    cpus=2.0,
    tmpfs_size="2g",
    timeout_seconds=1800,
    comparison=ML_REPLAY_POLICY,
)

PARSERS: dict[str, ParserImage] = {p.name: p for p in (MINERU, MARKER, DOCLING)}


def get_parser(name: str) -> ParserImage:
    try:
        return PARSERS[name]
    except KeyError:
        raise KeyError(
            f"unknown parser {name!r}; known parsers: {', '.join(sorted(PARSERS))}"
        ) from None

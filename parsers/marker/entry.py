"""Stele entry point for Marker (roadmap #10). Runs inside the parser image.

Output (under STELE_OUTPUT_DIR):
  document.md          Markdown rendering
  document.json        Marker's block tree: every page and block with its
                       polygon/bbox and block type
  images/...           extracted figures, referenced from document.md
  stele-parser.json    manifest (see parsers/README.md)

Runs Marker's "fast" mode with OCR disabled: layout and tables come from the
CPU rf-detr/onnx detectors and the PDF text layer. The VLM OCR model is not in
this image, so documents without a text layer (scans) are refused rather than
returned empty.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, "/opt/stele")
import stele_entry as se  # noqa: E402

DEFAULTS = {
    "mode": "fast",
    "disable_ocr": True,
    "extract_images": True,
}
ALLOWED = {
    "mode": (str,),
    "disable_ocr": (bool,),
    "extract_images": (bool,),
}
FORMATS = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".epub"}

MODELS_ROOT = Path(os.environ.get("STELE_MODELS_ROOT", "/opt/models"))


def convert(source: Path, out: Path, config: dict) -> dict:
    """Convert one document into out; returns manifest extras."""
    import torch
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict, shutdown_models
    from marker.renderers.json import JSONRenderer
    from marker.renderers.markdown import MarkdownRenderer

    torch.set_num_threads(se.thread_count())

    marker_config = {
        "mode": config["mode"],
        "disable_ocr": config["disable_ocr"],
        "disable_tqdm": True,
        "extract_images": config["extract_images"],
        # pdftext's worker pool stays within the CPU allowance.
        "pdftext_workers": se.thread_count(),
    }

    models = create_model_dict()
    try:
        converter = PdfConverter(artifact_dict=models, config=marker_config)
        document = converter.build_document(str(source))
        markdown = converter.resolve_dependencies(MarkdownRenderer)(document)
        blocks = converter.resolve_dependencies(JSONRenderer)(document)
    finally:
        shutdown_models(models)

    (out / "document.md").write_text(markdown.markdown, encoding="utf-8")
    (out / "document.json").write_text(
        blocks.model_dump_json(exclude=["metadata"], indent=2), encoding="utf-8"
    )
    if markdown.images:
        images = out / "images"
        images.mkdir()
        for name, image in markdown.images.items():
            # Marker names images relative to the markdown; keep only the
            # final component so nothing can escape the images directory.
            target = images / Path(name).name
            (image if image.mode == "RGB" else image.convert("RGB")).save(target)
        # Point the markdown at images/.
        text = markdown.markdown
        for name in markdown.images:
            text = text.replace(f"({name})", f"(images/{Path(name).name})")
        (out / "document.md").write_text(text, encoding="utf-8")
    return {"pages": len(document.pages)}


def run() -> None:
    source = se.input_path()
    out = se.output_dir()
    config = se.parser_config(DEFAULTS, ALLOWED)
    if source.suffix.lower() not in FORMATS:
        se.fail(se.EXIT_BAD_INPUT, f"Marker image does not accept {source.suffix or 'this file'}")
    if config["mode"] != "fast" or not config["disable_ocr"]:
        se.fail(
            se.EXIT_BAD_INPUT,
            "this image runs Marker's fast, text-layer mode only "
            "(mode='fast', disable_ocr=true); the VLM OCR model is not included",
        )
    # Written by warmup.py once every lazily loaded model is in the image.
    se.require_models([MODELS_ROOT / ".stele-ready"])

    from importlib.metadata import version as dist_version

    extra = convert(source, out, config)
    if not (out / "document.md").read_text(encoding="utf-8").strip():
        se.fail(
            se.EXIT_PARSE_FAILED,
            "no text extracted; the document may be a scan, which needs OCR "
            "(not included in this image)",
        )
    se.write_manifest(
        out,
        parser="marker",
        version=dist_version("marker-pdf"),
        config=config,
        source=source,
        outputs={"markdown": "document.md", "structure": "document.json"},
        extra=extra,
    )


if __name__ == "__main__":
    se.main(run)
